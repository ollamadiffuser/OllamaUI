# api.py
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from typing import List, Optional, Dict, Any, Union
import uvicorn
from pydantic import BaseModel
import logging
import base64
import io
from PIL import Image
import json
import re
import asyncio
import httpx

# Import existing Ollama functionality
try:
    import ollama
except ImportError:
    raise ImportError("Please install ollama package with: pip install ollama")

# Import RAG functionality
try:
    from rag_helper import get_rag_manager
except ImportError:
    logging.error("RAG dependencies not available. Install with: pip install chromadb langchain langchain-community")
    get_rag_manager = None

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(title="Ollama WebUI API")

# Image generation service configuration
IMAGE_SERVICE_URL = "http://localhost:8001"
MCP_SERVER_URL = "http://localhost:8002"

# Configure CORS for frontend connection
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], # Your Next.js dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Provider and Model Definitions ---
# This is from the original app.py
PREDEFINED_PROVIDERS = {
    "ollama": {
        "fetch_func": lambda: get_ollama_models(),
        "prefix": "ollama/", # LiteLLM generally expects ollama/model_name
        "dynamic_fetch": True,
        "fallback_models": [
            "devstral:24b", "llama3.3:70b", "llama3.2:latest", "qwen3:32b", 
            "qwq:32b", "gemma3:27b", "deepseek-r1:14b", "qwen2.5vl:32b"
        ]
    },
    "openrouter": {
        "models": [
            "openai/gpt-4o",
            "google/gemini-pro-1.5",
            "mistralai/mistral-large-latest",
            "anthropic/claude-3-opus",
            "nousresearch/nous-hermes-2-mixtral-8x7b-dpo",
            "meta-llama/llama-3-70b-instruct",
            "fireworks/firefunction-v1" # Example with a different structure
        ],
        "prefix": "openrouter/", # LiteLLM expects this full prefix
        "dynamic_fetch": False
    },
    "openai": {
        "models": ["gpt-4", "gpt-4-turbo", "gpt-3.5-turbo", "gpt-4o"],
        "prefix": "", # LiteLLM recognizes these directly
        "dynamic_fetch": False
    },
    "groq": {
        "models": ["llama3-8b-8192", "llama3-70b-8192", "mixtral-8x7b-32768", "gemma-7b-it"],
        "prefix": "groq/",
        "dynamic_fetch": False
    },
    "anthropic": {
        "models": ["claude-3-opus-20240229", "claude-3-sonnet-20240229", "claude-3-haiku-20240307", "claude-2.1"],
        "prefix": "", # LiteLLM recognizes these directly
        "dynamic_fetch": False
    }
}

# Models that should display their thinking process
THINKING_MODELS = [
    "deepseek-r1:14b", "deepseek-r1:8b",  # Deepseek models often have thinking tags
    "think",  # Any model with "think" in the name
    "reasoning",  # Any model with "reasoning" in the name
    "cot"  # Chain of thought models
]

# --- Helper Functions ---
def get_ollama_models():
    """
    Fetch the latest list of models from Ollama.
    This function ensures we get fresh data each time it's called.
    """
    try:
        logging.info("Fetching fresh model list from Ollama...")
        
        # Call ollama.list() to get the most recent model list
        models_response = ollama.list()
        logging.debug(f"Raw Ollama response type: {type(models_response)}")
        
        model_names = []
        
        # Handle the newer ollama package response format (ListResponse object)
        if hasattr(models_response, 'models'):
            models_data = models_response.models
            logging.debug(f"Found {len(models_data)} models in response")
            
            for model in models_data:
                # Handle Model objects from the newer ollama package
                if hasattr(model, 'model'):
                    model_names.append(model.model)
                elif hasattr(model, 'name'):
                    model_names.append(model.name)
                elif isinstance(model, dict):
                    # Fallback for dict format
                    if "name" in model:
                        model_names.append(model["name"])
                    elif "model" in model:
                        model_names.append(model["model"])
        
        # Legacy handling for older ollama package versions (dict response)
        elif isinstance(models_response, dict):
            models_data = models_response.get("models", [])
            for m in models_data:
                if isinstance(m, dict):
                    if "name" in m:
                        model_names.append(m["name"])
                    elif "model" in m:
                        model_names.append(m["model"])
        
        # Remove duplicates and sort
        model_names = sorted(list(set(model_names)))
        
        if model_names:
            logging.info(f"Successfully fetched {len(model_names)} Ollama models: {model_names}")
        else:
            logging.warning("No Ollama models found. Make sure Ollama is running and has models installed.")
            
        return model_names
        
    except ConnectionError as e:
        logging.error(f"Connection error when fetching Ollama models. Is Ollama running? Error: {e}")
        return []
    except Exception as e:
        logging.error(f"Error fetching Ollama models: {e}", exc_info=True)
        return []

def get_models_for_provider(provider_name, force_refresh=False):
    if provider_name not in PREDEFINED_PROVIDERS:
        return []
    
    provider_info = PREDEFINED_PROVIDERS[provider_name]
    raw_models = []
    used_fallback = False

    if provider_info.get("dynamic_fetch", False):
        # Always fetch from API for dynamic providers (like Ollama) when force_refresh is True
        # or when it's the normal flow
        fetched_models = provider_info["fetch_func"]()
        if fetched_models:
            raw_models = fetched_models
        elif "fallback_models" in provider_info:
            logging.warning(f"Dynamic fetch for {provider_name} failed or returned no models. Using predefined fallback models for this provider.")
            raw_models = provider_info["fallback_models"]
            used_fallback = True
        else:
            logging.warning(f"Dynamic fetch for {provider_name} failed and no fallback models defined for this provider.")
            raw_models = []
    else:
        raw_models = provider_info.get("models", [])
    
    return raw_models

def image_to_base64(pil_image, format="JPEG"):
    if pil_image is None: return None
    buffered = io.BytesIO()
    pil_image.save(buffered, format=format)
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

def check_ollama_connection():
    """
    Check if Ollama is running and accessible.
    Returns True if connected, False otherwise.
    """
    try:
        # Try to get the list of models to test connection
        response = ollama.list()
        logging.info("Ollama connection successful")
        return True
    except Exception as e:
        logging.error(f"Ollama connection failed: {e}")
        return False

# --- API Data Models ---
class MessageContent(BaseModel):
    type: str  # "text" or "image_url"
    text: Optional[str] = None
    image_url: Optional[Dict[str, str]] = None
    
class Message(BaseModel):
    role: str
    content: Union[str, List[MessageContent], List[Dict[str, Any]]]
    
class ChatRequest(BaseModel):
    model: str
    messages: List[Message]
    system_prompt: Optional[str] = None
    temperature: float = 0.7
    stream: bool = False
    
class ChatResponse(BaseModel):
    message: Message
    model: str

# RAG Request Models
class DocumentUploadResponse(BaseModel):
    success: bool
    message: str
    filename: Optional[str] = None

class RAGQueryRequest(BaseModel):
    query: str
    model: Optional[str] = None
    
class RAGQueryResponse(BaseModel):
    response: str
    sources: List[dict]

# Image Generation Request Models
class ImageGenerationRequest(BaseModel):
    prompt: str
    negative_prompt: Optional[str] = "low quality, bad anatomy, worst quality, low resolution"
    num_inference_steps: int = 8
    guidance_scale: float = 1.5

class ImageGenerationResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    image_url: Optional[str] = None

# --- API Endpoints ---
@app.get("/api/providers")
async def get_providers():
    providers = []
    for provider_key in PREDEFINED_PROVIDERS.keys():
        # Create provider object with id and name
        provider_name = provider_key.title()  # Capitalize first letter
        if provider_key == "openrouter":
            provider_name = "OpenRouter"
        elif provider_key == "openai":
            provider_name = "OpenAI"
        providers.append({
            "id": provider_key,
            "name": provider_name
        })
    return {"providers": providers}

@app.get("/api/ollama/status")
async def get_ollama_status():
    """Check if Ollama service is running and accessible."""
    is_connected = check_ollama_connection()
    return {
        "connected": is_connected,
        "service": "ollama"
    }

@app.get("/api/models/{provider}")
async def get_provider_models(provider: str, t: Optional[str] = None, force_refresh: bool = False):
    """Get models for a specific provider. 
    
    Args:
        provider: The provider name (e.g., 'ollama', 'openai')
        t: Timestamp parameter for cache busting (optional)
        force_refresh: Force refresh from the provider API (optional)
    """
    # For debugging: log when refresh is requested
    if force_refresh or t:
        logging.info(f"Force refresh requested for {provider} provider (t={t}, force_refresh={force_refresh})")
    
    # For Ollama, check connection first when force_refresh is requested
    if provider == 'ollama' and force_refresh:
        if not check_ollama_connection():
            raise HTTPException(
                status_code=503, 
                detail="Ollama service is not running or accessible. Please start Ollama and try again."
            )
    
    models = get_models_for_provider(provider, force_refresh=force_refresh)
    if not models:
        if provider == 'ollama':
            raise HTTPException(
                status_code=404, 
                detail="No Ollama models found. Make sure Ollama is running and has models installed. Try running 'ollama pull llama3' to install a model."
            )
        else:
            raise HTTPException(status_code=404, detail=f"No models found for provider {provider}")
    return {"models": models}

@app.post("/api/chat")
async def chat(request: ChatRequest):
    try:
        # Check if the latest message is an image generation command
        if request.messages and len(request.messages) > 0:
            latest_message = request.messages[-1]
            message_text = ""
            
            # Extract text from message content
            if isinstance(latest_message.content, str):
                message_text = latest_message.content
            elif isinstance(latest_message.content, list):
                for content_item in latest_message.content:
                    if isinstance(content_item, MessageContent) and content_item.text:
                        message_text += content_item.text + " "
                    elif isinstance(content_item, dict) and content_item.get("text"):
                        message_text += content_item["text"] + " "
            
            message_text = message_text.strip()
            
            # Check for image generation commands
            image_commands = ["/imagine", "/generate", "/image", "/draw", "/create-image", "/pic"]
            if any(message_text.lower().startswith(cmd) for cmd in image_commands):
                # Extract the prompt after the command
                for cmd in image_commands:
                    if message_text.lower().startswith(cmd):
                        prompt = message_text[len(cmd):].strip()
                        if not prompt:
                            return {
                                "message": {
                                    "role": "assistant",
                                    "content": f"Please provide a description for the image you want to generate. For example: `{cmd} a beautiful sunset over mountains`"
                                },
                                "model": request.model
                            }
                        
                        # Generate the image
                        try:
                            logging.info(f"Generating image for prompt: {prompt[:100]}...")
                            
                            async with httpx.AsyncClient(timeout=300.0) as client:
                                image_payload = {
                                    "prompt": prompt,
                                    "negative_prompt": "low quality, bad anatomy, worst quality, low resolution",
                                    "num_inference_steps": 8,
                                    "guidance_scale": 1.5
                                }
                                
                                response = await client.post(f"{IMAGE_SERVICE_URL}/predict", json=image_payload)
                                
                                if response.status_code == 200:
                                    # Convert image to base64 for embedding in chat
                                    import base64
                                    image_b64 = base64.b64encode(response.content).decode('utf-8')
                                    
                                    return {
                                        "message": {
                                            "role": "assistant",
                                            "content": f"I've generated an image for: **{prompt}**",
                                            "image": f"data:image/png;base64,{image_b64}"
                                        },
                                        "model": request.model,
                                        "image_generated": True
                                    }
                                else:
                                    return {
                                        "message": {
                                            "role": "assistant",
                                            "content": f"Sorry, I couldn't generate an image for '{prompt}'. The image generation service returned an error. Please try again or check if the image generation service is running."
                                        },
                                        "model": request.model
                                    }
                        
                        except httpx.ConnectError:
                            return {
                                "message": {
                                    "role": "assistant",
                                    "content": "Sorry, the image generation service is not available. Please make sure it's running and try again."
                                },
                                "model": request.model
                            }
                        except Exception as e:
                            logging.error(f"Error generating image in chat: {str(e)}")
                            return {
                                "message": {
                                    "role": "assistant",
                                    "content": f"Sorry, there was an error generating the image: {str(e)}"
                                },
                                "model": request.model
                            }
        
        # Regular chat processing
        from litellm import completion
        
        # Process messages to ensure they're in the correct format for LiteLLM
        processed_messages = []
        for msg in request.messages:
            processed_msg = {"role": msg.role}
            
            # Handle different content types
            if isinstance(msg.content, str):
                processed_msg["content"] = msg.content
            elif isinstance(msg.content, list):
                processed_content = []
                for content_item in msg.content:
                    if isinstance(content_item, MessageContent):
                        # Convert Pydantic object to dict
                        content_dict = {"type": content_item.type}
                        if content_item.text is not None:
                            content_dict["text"] = content_item.text
                        if content_item.image_url is not None:
                            content_dict["image_url"] = content_item.image_url
                        processed_content.append(content_dict)
                    elif isinstance(content_item, dict):
                        # Already a dict, use as-is
                        processed_content.append(content_item)
                    else:
                        # Fallback for other types
                        processed_content.append(str(content_item))
                processed_msg["content"] = processed_content
            else:
                # Fallback for other content types
                processed_msg["content"] = str(msg.content)
            
            processed_messages.append(processed_msg)
        
        # Process the request using LiteLLM
        response = completion(
            model=request.model,
            messages=processed_messages,
            temperature=request.temperature,
            stream=False
        )
        
        # Extract the response content
        response_content = ""
        if hasattr(response, 'choices') and response.choices:
            if hasattr(response.choices[0], 'message'):
                response_content = response.choices[0].message.content
            elif hasattr(response.choices[0], 'text'):
                response_content = response.choices[0].text
        elif hasattr(response, 'content'):
            response_content = response.content
        elif hasattr(response, 'text'):
            response_content = response.text
        
        return {
            "message": {
                "role": "assistant",
                "content": response_content
            },
            "model": request.model
        }
    except Exception as e:
        logging.error(f"Error in chat: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/api/chat/stream")
async def chat_stream(websocket: WebSocket):
    try:
        await websocket.accept()
        logging.info("WebSocket connection accepted")
        
        # Receive the initial chat request
        data = await websocket.receive_text()
        request_data = json.loads(data)
        logging.info(f"Received WebSocket request: {request_data}")
        
        try:
            chat_request = ChatRequest(**request_data)
        except Exception as validation_error:
            error_msg = f"Invalid request format: {str(validation_error)}"
            logging.error(error_msg)
            await websocket.send_json({
                "error": error_msg
            })
            await websocket.close()
            return
        
        # Force stream to true for WebSocket API
        chat_request.stream = True
        
        # Check for image generation commands in streaming chat
        if chat_request.messages and len(chat_request.messages) > 0:
            latest_message = chat_request.messages[-1]
            message_text = ""
            
            # Extract text from message content
            if isinstance(latest_message.content, str):
                message_text = latest_message.content
            elif isinstance(latest_message.content, list):
                for content_item in latest_message.content:
                    if isinstance(content_item, MessageContent) and content_item.text:
                        message_text += content_item.text + " "
                    elif isinstance(content_item, dict) and content_item.get("text"):
                        message_text += content_item["text"] + " "
            
            message_text = message_text.strip()
            
            # Check for image generation commands
            image_commands = ["/imagine", "/generate", "/image", "/draw", "/create-image", "/pic"]
            if any(message_text.lower().startswith(cmd) for cmd in image_commands):
                # Extract the prompt after the command
                for cmd in image_commands:
                    if message_text.lower().startswith(cmd):
                        prompt = message_text[len(cmd):].strip()
                        if not prompt:
                            await websocket.send_json({
                                "chunk": f"Please provide a description for the image you want to generate. For example: `{cmd} a beautiful sunset over mountains`",
                                "message": {
                                    "role": "assistant",
                                    "content": f"Please provide a description for the image you want to generate. For example: `{cmd} a beautiful sunset over mountains`"
                                }
                            })
                            await websocket.send_json({
                                "done": True,
                                "message": {
                                    "role": "assistant",
                                    "content": f"Please provide a description for the image you want to generate. For example: `{cmd} a beautiful sunset over mountains`"
                                },
                                "model": chat_request.model
                            })
                            return
                        
                        # Send initial message
                        await websocket.send_json({
                            "chunk": f"🎨 Generating image for: {prompt}...",
                            "message": {
                                "role": "assistant",
                                "content": f"🎨 Generating image for: {prompt}..."
                            }
                        })
                        
                        # Generate the image
                        try:
                            logging.info(f"Generating image for prompt in stream: {prompt[:100]}...")
                            
                            async with httpx.AsyncClient(timeout=300.0) as client:
                                image_payload = {
                                    "prompt": prompt,
                                    "negative_prompt": "low quality, bad anatomy, worst quality, low resolution",
                                    "num_inference_steps": 8,
                                    "guidance_scale": 1.5
                                }
                                
                                response = await client.post(f"{IMAGE_SERVICE_URL}/predict", json=image_payload)
                                
                                if response.status_code == 200:
                                    # Convert image to base64 for embedding in chat
                                    import base64
                                    image_b64 = base64.b64encode(response.content).decode('utf-8')
                                    
                                    final_content = f"I've generated an image for: **{prompt}**"
                                    
                                    await websocket.send_json({
                                        "done": True,
                                        "message": {
                                            "role": "assistant",
                                            "content": final_content,
                                            "image": f"data:image/png;base64,{image_b64}"
                                        },
                                        "model": chat_request.model,
                                        "image_generated": True
                                    })
                                else:
                                    error_msg = f"Sorry, I couldn't generate an image for '{prompt}'. Please try again."
                                    await websocket.send_json({
                                        "done": True,
                                        "message": {
                                            "role": "assistant",
                                            "content": error_msg
                                        },
                                        "model": chat_request.model
                                    })
                        
                        except httpx.ConnectError:
                            error_msg = "Sorry, the image generation service is not available. Please make sure it's running and try again."
                            await websocket.send_json({
                                "done": True,
                                "message": {
                                    "role": "assistant",
                                    "content": error_msg
                                },
                                "model": chat_request.model
                            })
                        except Exception as e:
                            logging.error(f"Error generating image in stream: {str(e)}")
                            error_msg = f"Sorry, there was an error generating the image: {str(e)}"
                            await websocket.send_json({
                                "done": True,
                                "message": {
                                    "role": "assistant",
                                    "content": error_msg
                                },
                                "model": chat_request.model
                            })
                        return
            
        try:
            from litellm import completion
            
            logging.info(f"Starting streaming completion for model: {chat_request.model}")
            
            # Process messages to ensure they're in the correct format for LiteLLM
            processed_messages = []
            for msg in chat_request.messages:
                processed_msg = {"role": msg.role}
                
                # Handle different content types
                if isinstance(msg.content, str):
                    processed_msg["content"] = msg.content
                elif isinstance(msg.content, list):
                    processed_content = []
                    for content_item in msg.content:
                        if isinstance(content_item, MessageContent):
                            # Convert Pydantic object to dict
                            content_dict = {"type": content_item.type}
                            if content_item.text is not None:
                                content_dict["text"] = content_item.text
                            if content_item.image_url is not None:
                                content_dict["image_url"] = content_item.image_url
                            processed_content.append(content_dict)
                        elif isinstance(content_item, dict):
                            # Already a dict, use as-is
                            processed_content.append(content_item)
                        else:
                            # Fallback for other types
                            processed_content.append(str(content_item))
                    processed_msg["content"] = processed_content
                else:
                    # Fallback for other content types
                    processed_msg["content"] = str(msg.content)
                
                processed_messages.append(processed_msg)
            
            # Stream the chat response
            response_stream = completion(
                model=chat_request.model,
                messages=processed_messages,
                temperature=chat_request.temperature,
                stream=True
            )
            
            full_response = ""
            
            for chunk in response_stream:
                delta = None
                
                # Handle different response formats
                if hasattr(chunk, 'choices') and chunk.choices:
                    if hasattr(chunk.choices[0], 'delta'):
                        if hasattr(chunk.choices[0].delta, 'content') and chunk.choices[0].delta.content is not None:
                            delta = chunk.choices[0].delta.content
                        elif hasattr(chunk.choices[0].delta, 'text') and chunk.choices[0].delta.text is not None:
                            delta = chunk.choices[0].delta.text
                    elif hasattr(chunk.choices[0], 'text') and chunk.choices[0].text is not None:
                        delta = chunk.choices[0].text
                    elif hasattr(chunk.choices[0], 'message') and hasattr(chunk.choices[0].message, 'content') and chunk.choices[0].message.content is not None:
                        delta = chunk.choices[0].message.content
                
                # Skip empty deltas
                if not delta:
                    continue
                
                # Detect and handle <think> tags while streaming
                full_response += delta
                
                # Only send non-thinking content
                await websocket.send_json({
                    "chunk": delta,
                    "message": {
                        "role": "assistant",
                        "content": delta
                    }
                })
                
                # Small delay to not overwhelm the client
                await asyncio.sleep(0.01)
            
            # Send final message
            await websocket.send_json({
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": full_response
                },
                "model": chat_request.model
            })
            
            logging.info("Streaming completed successfully")
                
        except Exception as e:
            error_msg = f"Streaming error: {str(e)}"
            logging.error(error_msg, exc_info=True)
            await websocket.send_json({"error": error_msg})
    
    except WebSocketDisconnect:
        logging.info("WebSocket disconnected")
    except Exception as e:
        logging.error(f"WebSocket error: {str(e)}", exc_info=True)
    finally:
        # Ensure the connection is closed properly
        try:
            await websocket.close()
        except:
            pass

# RAG Endpoints
@app.post("/api/rag/upload", response_model=DocumentUploadResponse)
async def upload_document(file: UploadFile = File(...)):
    """Upload a document to the RAG knowledge base"""
    if not get_rag_manager:
        raise HTTPException(status_code=500, detail="RAG functionality not available")
    
    try:
        # Check file type
        if not file.filename.lower().endswith(('.pdf', '.txt')):
            return DocumentUploadResponse(
                success=False,
                message="Only PDF and TXT files are supported"
            )
        
        # Read file content
        content = await file.read()
        
        # Get file extension
        file_type = "pdf" if file.filename.lower().endswith('.pdf') else "txt"
        
        # Add to RAG system
        rag_manager = get_rag_manager()
        success = rag_manager.add_document(content, file.filename, file_type)
        
        if success:
            return DocumentUploadResponse(
                success=True,
                message=f"Document '{file.filename}' uploaded successfully",
                filename=file.filename
            )
        else:
            return DocumentUploadResponse(
                success=False,
                message=f"Failed to upload document '{file.filename}'"
            )
            
    except Exception as e:
        logging.error(f"Error uploading document: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rag/query", response_model=RAGQueryResponse)
async def rag_query(request: RAGQueryRequest):
    """Query the RAG system with context from uploaded documents"""
    if not get_rag_manager:
        raise HTTPException(status_code=500, detail="RAG functionality not available")
    
    try:
        rag_manager = get_rag_manager()
        
        # Clean model name - remove "ollama/" prefix if present
        model_name = request.model
        if model_name and model_name.startswith("ollama/"):
            model_name = model_name.replace("ollama/", "")
        
        # If no model provided, use a default from available models
        if not model_name:
            # Get available models and pick a good default
            available_models = get_models_for_provider('ollama')
            if available_models:
                # Prefer larger models for better RAG responses
                preferred_models = ['llama3.3:70b', 'qwen3:32b', 'qwen2.5vl:32b', 'gemma3:27b', 'devstral:24b']
                for preferred in preferred_models:
                    if preferred in available_models:
                        model_name = preferred
                        break
                if not model_name:
                    model_name = available_models[0]  # Use first available as fallback
            else:
                raise HTTPException(status_code=500, detail="No models available for RAG")
        
        # Get relevant documents
        sources = rag_manager.search_documents(request.query, n_results=3)
        
        # Generate RAG response
        response = rag_manager.generate_rag_response(request.query, model_name)
        
        return RAGQueryResponse(
            response=response,
            sources=sources
        )
        
    except Exception as e:
        logging.error(f"Error in RAG query: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/rag/documents")
async def list_documents():
    """List all documents in the RAG knowledge base"""
    if not get_rag_manager:
        raise HTTPException(status_code=500, detail="RAG functionality not available")
    
    try:
        rag_manager = get_rag_manager()
        documents = rag_manager.list_documents()
        return {"documents": documents}
        
    except Exception as e:
        logging.error(f"Error listing documents: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/rag/documents/{filename}")
async def delete_document(filename: str):
    """Delete a document from the RAG knowledge base"""
    if not get_rag_manager:
        raise HTTPException(status_code=500, detail="RAG functionality not available")
    
    try:
        rag_manager = get_rag_manager()
        success = rag_manager.delete_document(filename)
        
        if success:
            return {"message": f"Document '{filename}' deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail=f"Document '{filename}' not found")
            
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error deleting document: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/rag/status")
async def rag_status():
    """Check RAG system status"""
    if not get_rag_manager:
        return {
            "available": False,
            "error": "RAG dependencies not installed"
        }
    
    try:
        rag_manager = get_rag_manager()
        embedding_available = rag_manager.check_embedding_model_available()
        
        return {
            "available": True,
            "embedding_model": rag_manager.embedding_model,
            "embedding_model_available": embedding_available,
            "collection_name": rag_manager.collection_name
        }
        
    except Exception as e:
        logging.error(f"Error checking RAG status: {str(e)}")
        return {
            "available": False,
            "error": str(e)
        }

# Image Generation Endpoints
@app.get("/api/image/status")
async def image_generation_status():
    """Check if image generation service is running"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{IMAGE_SERVICE_URL}/health", timeout=5.0)
            return {
                "available": response.status_code == 200,
                "service": "stable-diffusion-3.5"
            }
    except Exception as e:
        logging.error(f"Image generation service not available: {str(e)}")
        return {
            "available": False,
            "error": str(e)
        }

@app.post("/api/image/generate")
async def generate_image(request: ImageGenerationRequest):
    """Generate an image using Stable Diffusion"""
    try:
        # Prepare request for image generation service
        payload = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "num_inference_steps": request.num_inference_steps,
            "guidance_scale": request.guidance_scale
        }
        
        logging.info(f"Generating image for prompt: {request.prompt[:100]}...")
        
        async with httpx.AsyncClient(timeout=300.0) as client:  # 5 minutes timeout for image generation
            response = await client.post(f"{IMAGE_SERVICE_URL}/predict", json=payload)
            
            if response.status_code == 200:
                # Return the image directly
                return Response(
                    content=response.content,
                    media_type="image/png",
                    headers={
                        "Content-Disposition": "inline; filename=generated_image.png"
                    }
                )
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Image generation failed: {response.text}"
                )
                
    except httpx.TimeoutException:
        logging.error("Image generation timeout")
        raise HTTPException(status_code=504, detail="Image generation timeout")
    except httpx.ConnectError:
        logging.error("Cannot connect to image generation service")
        raise HTTPException(
            status_code=503, 
            detail="Image generation service unavailable. Make sure the service is running on port 8001."
        )
    except Exception as e:
        logging.error(f"Error generating image: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# MCP Proxy Endpoints - Forward requests to MCP server
@app.get("/api/mcp/status")
async def mcp_status():
    """Proxy MCP status check to MCP server"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{MCP_SERVER_URL}/api/mcp/status", timeout=5.0)
            return response.json()
    except Exception as e:
        logging.error(f"MCP status check failed: {str(e)}")
        return {"connected": False, "error": str(e)}

@app.get("/api/mcp/servers")
async def mcp_servers():
    """Proxy MCP servers list to MCP server"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{MCP_SERVER_URL}/api/mcp/servers", timeout=10.0)
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(status_code=response.status_code, detail=response.text)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="MCP server not available")
    except Exception as e:
        logging.error(f"MCP servers request failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/mcp/tools")
async def mcp_tools():
    """Proxy MCP tools list to MCP server"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{MCP_SERVER_URL}/api/mcp/tools", timeout=10.0)
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(status_code=response.status_code, detail=response.text)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="MCP server not available")
    except Exception as e:
        logging.error(f"MCP tools request failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/mcp/resources")
async def mcp_resources(uri: Optional[str] = None):
    """Proxy MCP resources request to MCP server"""
    try:
        params = {"uri": uri} if uri else {}
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{MCP_SERVER_URL}/api/mcp/resources", params=params, timeout=10.0)
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(status_code=response.status_code, detail=response.text)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="MCP server not available")
    except Exception as e:
        logging.error(f"MCP resources request failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/mcp/tools/call")
async def mcp_tool_call(request: dict):
    """Proxy MCP tool call to MCP server"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{MCP_SERVER_URL}/api/mcp/tools/call", 
                json=request, 
                timeout=30.0
            )
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(status_code=response.status_code, detail=response.text)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="MCP server not available")
    except Exception as e:
        logging.error(f"MCP tool call failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)