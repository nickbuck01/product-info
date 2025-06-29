import mimetypes
import re
import os
import requests
from pydantic import BaseModel
from fastapi import Request, APIRouter
from fastapi.logger import logger
from openai import OpenAI
from requests import Timeout
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from services.ai_responses import get_ai_response, add_message_to_session, get_or_create_session, send_whatsapp

whatsapp = APIRouter(
    prefix="/v1"
)

account_sid = os.environ.get("ACCOUNT_SID")
auth_token = os.environ.get("AUTH_TOKEN")
twilio_number_code = os.environ.get("TWILIO_NUMBER_CODE")
OPEN_AI_API_KEY = os.environ.get("OPEN_AI_API_KEY")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")


twilio_client = Client(account_sid, auth_token)
client = OpenAI(
    api_key=OPEN_AI_API_KEY,
)


# Define a route for handling incoming WhatsApp messages
@whatsapp.post('/whatsapp')
async def whatsapp_endpoint(request: Request):
    """
    :param request: Request object from Twilio
    :return: Message acknowledgement with status 200
    """
    try:
        form = await request.form()
        user_message = form['Body'].lower()  # User's message
        user_number = form['From']

        media_url = form.get("MediaUrl0")  # For media (voice recordings)
        media_content_type = form.get("MediaContentType0")  # Content type (e.g., audio/ogg)
        session_id = get_or_create_session(user_number)
        if user_message:
            logger.info(f"User message (Text) : {user_message}")
            responses, tool_output = get_ai_response(user_phone=user_number, message=user_message, session_id=session_id)

        elif media_url and media_content_type.startswith("audio/"):
            # Handle voice message (requires transcription)
            user_message = transcribe_audio(media_url)
            if user_message:
                logger.info(f"User message (Audio) : {user_message}")
                responses, tool_output = get_ai_response(user_phone=user_number, message=user_message, session_id=session_id)
            else:
                responses = ["Could not transcribe voice message."]

        logger.info(f"Received request with query - {user_message}")

        pattern = r'【\d+:\d+†[^\]]+】'
        for response in responses:
            response = re.sub(pattern, '', response)
            add_message_to_session(user_number, session_id, response, sender="assistant")
            if not tool_output:
                send_whatsapp(to=user_number, body=response)

        return MessagingResponse()
    except Exception as e:
        logger.error(f"Got an exception - {e}")


class WhatsAppRequest(BaseModel):
    From: str
    Body: str
    isWebRequest: bool


def transcribe_audio(media_url):
    auth = (account_sid, auth_token)
    try:
        response = requests.get(media_url, auth=auth, timeout=10)
        response.raise_for_status()
    except Timeout:
        return None
    except Exception as e:
        logger.exception(f"Error while transcribing audio {e}")

    content_type = response.headers['Content-Type']
    ext = mimetypes.guess_extension(content_type)
    if ext not in ['.flac', '.m4a', '.mp3', '.mp4', '.mpeg', '.mpga', '.oga', '.ogg', '.wav', '.webm']:
        return None

    temp_audio_file = f"temp_audio{ext}"
    with open(temp_audio_file, 'wb') as f:
        f.write(response.content)

    with open(temp_audio_file, 'rb') as f:
        response = client.audio.transcriptions.create(model="whisper-1", file=f)
        logger.info(f"Transcribed audio message as - {response.text}")
        return response.text
