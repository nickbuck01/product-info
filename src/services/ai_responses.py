import re
import os
import time
import uuid
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import json
from email.mime.multipart import MIMEMultipart
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.mime.text import MIMEText
import base64
from fastapi.logger import logger
from openai import OpenAI
from twilio.rest import Client
from sqlalchemy import create_engine, text


OPEN_AI_API_KEY = os.environ.get("OPEN_AI_API_KEY")
ORGANIZATION = os.environ.get("ORGANIZATION")
PROJECT_ID = os.environ.get("PROJECT_ID")
ASSISTANT_ID = os.environ.get("ASSISTANT_ID")
GOOGLE_SERVICE_ACCOUNT_URL = os.environ.get("GOOGLE_SERVICE_ACCOUNT_URL")
GOOGLE_SHEET_URL_FOR_UNANSWERED_QUESTIONS = os.environ.get("GOOGLE_SHEET_URL_FOR_UNANSWERED_QUESTIONS")
GOOGLE_SHEET_URL_FOR_CALL_BACK_REQUESTS = os.environ.get("GOOGLE_SHEET_URL_FOR_CALL_BACK_REQUESTS")
IMPERSONATED_USER = os.environ.get("IMPERSONATED_USER")
EMAIL_TO_USER = os.environ.get("EMAIL_TO_USER")
SATORIAI_GMAIL_KEY = os.environ.get("SATORIAI_GMAIL_KEY")

account_sid = os.environ.get("account_sid")
auth_token = os.environ.get("auth_token")

TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")

DB_URL = os.environ.get("DB_URL")

SCOPES = ['https://mail.google.com/'] #['https://www.googleapis.com/auth/gmail.send']

user_thread_info = {}
sessions = {}
TIMEOUT_DURATION = timedelta(minutes=1)

twilio_client = Client(account_sid, auth_token)
client = OpenAI(
  api_key=OPEN_AI_API_KEY,
  organization=ORGANIZATION,
  project=PROJECT_ID,
)


def get_ai_response(user_phone, message, session_id):
    """
    this function will generate the response for the given query
    """
    add_message_to_session(user_phone, session_id, message)
    thread_id = add_message_in_thread(user_phone, message)
    text, tool_output = run_thread(thread_id, user_phone)
    return text, tool_output


def add_message_in_thread(user_phone, message):
    """
    this function will add the user's message in thread, if thread is not
     already created, it will create a thread and then put new message in it
    """
    thread_id = user_thread_info.get(user_phone)
    if not thread_id:
        new_thread = client.beta.threads.create()
        thread_id = new_thread.id
        user_thread_info[user_phone] = thread_id

        client.beta.threads.messages.create(
            thread_id,
            role="user",
            content='''TyserAllen Client-Facing Assistant Prompt (for Assistants API):

                        You are a friendly and helpful assistant representing TyserAllen. Your primary role is to support clients with selecting fabric swatches for drapes. You have access to a list of available fabrics, including their type, color, and an associated swatch image.
                        
                        When a client confirms they are happy with a specific fabric or color, you should prepare clear and polite messages suitable for both WhatsApp and email. These messages should include the fabric type, color name, and the corresponding swatch image path from the dataset. Your responses should be courteous, warm, and professional.
                        
                        If a client asks a question about drape specifications or anything outside fabric selection (such as material properties, measurements, or installation), politely inform them:
                        "I'm here to help with fabric selection only, so I'm not able to answer that. One of the team will be happy to assist you with those details."
                        
                        To fetch the list of colors call the function get_color_list. This will return all the colors available in stock.
                        
                        To fetch the information of a particular color, call the function get_product_info along with color, fabric type and user_email. 
                        Consider fabric type as VELVET by default always.
                        
                        Ask for the email of the user to send the product on user email.
                    ''',
        )
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )
        time.sleep(5)
    client.beta.threads.messages.create(
        thread_id,
        role="user",
        content=message,
    )
    return thread_id


def run_thread(thread_id, user_phone):
    """
    this function will finally run the thread and return the generated response
    """
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID
    )
    tool_outputs = []
    while True:
        run = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id
        )

        if run.status == "completed":
            logger.info("Run completed")
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            latest_message = messages.data[0]
            text = latest_message.content[0].text.value
            split_text = split_response(text)
            return split_text, False

        elif run.status == 'requires_action':
            logger.info("Run required action")
            for function_call in run.required_action.submit_tool_outputs.tool_calls:
                if function_call.function.name == "get_color_list":
                    colors_list = get_color_list()

                    tool_outputs.append({
                        "tool_call_id":
                            function_call.id,
                        "output":
                            colors_list
                    })
                    send_whatsapp(to=f"{user_phone}",
                                  body=f'These are the available colours available in stock currently - {colors_list}')
                elif function_call.function.name == "get_product_info":
                    arguments = json.loads(function_call.function.arguments)
                    fabric_list = get_product_info(fabric=arguments.get("fabric"), color=arguments.get("color"))

                    tool_outputs.append({
                        "tool_call_id":
                            function_call.id,
                        "output":
                            "Fabric list sent"
                    })

                    gmail_service = create_gmail_service()
                    for fabric in fabric_list:
                        send_whatsapp(to=f"{user_phone}",
                                      body=f'{fabric.get("fabric").upper()} {fabric.get("color").upper()}',
                                      image_url=f'{fabric.get("swatch_image")}')
                        send_email(
                            gmail_service,
                            sender=IMPERSONATED_USER,  # The email account to send from (same as IMPERSONATED_USER)
                            to=arguments.get("user_email"),  # Recipient email address
                            subject='Tyser Allan Product Information',
                            body=f"""
                                <html>
                                  <body style="font-family: Arial, sans-serif;">
                                    <div style="text-align: center;">
                                        <img src="https://res.cloudinary.com/djjny1dxb/image/upload/v1749386230/tyser_allan_logo_hxtqfx.jpg" style="max-width: 70%; height: auto; margin-top: 20px;" />
                                    </div>
                                    <div style="text-align: center;">
                                      <h2 style="border: 1px solid #000; display: inline-block; padding: 5px 20px;">PRODUCT INFO</h2>
                                    </div>
                                    <h1 style="text-align: center; margin-top: 30px;">{fabric.get("fabric").upper()} {fabric.get("color").upper()}:</h1>
                                    <div style="text-align: center;">
                                        <img src="{fabric.get("swatch_image")}" style="max-width: 80%; height: auto; margin-top: 20px;" />
                                    </div>
                                  </body>
                                </html>
                                """
                            )
                elif function_call.function.name == "call_back_request":
                    arguments = json.loads(function_call.function.arguments)

                    tool_outputs.append({
                        "tool_call_id":
                            function_call.id,
                        "output":
                            "Call back details saved to Google Sheets."
                    })
                    text = ["We have logged your details, you will soon get a call from one of our executives."]

                    gmail_service = create_gmail_service()
                    send_email(
                        gmail_service,
                        sender=IMPERSONATED_USER,  # The email account to send from (same as IMPERSONATED_USER)
                        to=EMAIL_TO_USER,  # Recipient email address
                        subject='Call back request',
                        body=f"""
                            Hello,
                            
                            A customer has requested a call back. 
                            
                            Below are the details:
                            
                            Last name: {arguments.get('last_name', '')}
                            Phone: {user_phone.split(":")[-1]}
                            Treatment: {arguments.get('treatment', ''),}
                            
                            Best regards,
                            Skin Clinic London
                            """
                    )
                elif function_call.function.name == "handle_unanswered_question":
                    print(f"handle_unanswered_question - {function_call.function.arguments}")
                    arguments = json.loads(function_call.function.arguments)
                    print(f"arguments - {arguments}")
                    unanswered_question = arguments.get("question")
                    save_unanswered_questions_to_sheet(unanswered_question, user_phone)

                    tool_outputs.append({
                        "tool_call_id":
                            function_call.id,
                        "output":
                            "Unanswered question noted and saved to Google Sheets."
                    })
                    text = "Sorry I have not been taught how to answer that question. " \
                           "We will make a note of that question and I will be able to answer it soon."
                    send_whatsapp(to=f"{user_phone}",
                                  body=text)

            if tool_outputs:
                completed_run = client.beta.threads.runs.submit_tool_outputs_and_poll(
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=tool_outputs)
                messages = client.beta.threads.messages.list(thread_id=thread_id)
                latest_message = messages.data[0]
                final_response = latest_message.content[0].text.value
                final_response = split_response(final_response)
            return final_response, True


# Google Sheets setup and authorization
def save_unanswered_questions_to_sheet(unanswered_question, user_phone):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials_info = json.loads(base64.b64decode(GOOGLE_SERVICE_ACCOUNT_URL))

    # Create the credentials object from the JSON dictionary
    creds = ServiceAccountCredentials.from_json_keyfile_dict(credentials_info, scope)
    client = gspread.authorize(creds)
    # Open the specific Google Sheet (worksheet 1 assumed for unanswered questions)
    sheet = client.open_by_url(GOOGLE_SHEET_URL_FOR_UNANSWERED_QUESTIONS)
    sheet = sheet.sheet1
    # Append each unanswered question to the sheet
    user_phone = user_phone.split(":")[-1]
    sheet.append_row([unanswered_question, user_phone])
    logger.info("Unanswered questions saved to Google Sheets.")


def get_or_create_session(user_id):
    """Get or create a session for the user."""
    if user_id not in sessions:
        sessions[user_id] = []
    if not sessions[user_id] or datetime.now() - sessions[user_id][-1]["end_time"] > TIMEOUT_DURATION:
        session_id = str(uuid.uuid4())
        new_session = {
            "session_id": session_id,
            "start_time": datetime.now(),
            "end_time": datetime.now(),
            "messages": []
        }
        sessions[user_id].append(new_session)
        return session_id
    return sessions[user_id][-1]["session_id"]


def add_message_to_session(user_number, session_id, message_text, sender="user"):
    """
    Add a message to the session with sender information.
    """
    for session in sessions[user_number]:
        if session["session_id"] == session_id:
            session["messages"].append({
                "timestamp": datetime.now(),
                "text": message_text,
                "sender": sender
            })
            session["end_time"] = datetime.now()


def split_response(text, max_length=800):
    """
    Splits a lengthy assistant response into sections that respect sentence
    boundaries and do not exceed max_length characters.
    """
    parts = []
    while len(text) > max_length:
        # Find the nearest sentence boundary within max_length
        split_index = max_length
        sentence_end = re.search(r'(?<=\.)\s|\n', text[:max_length][::-1])

        if sentence_end:
            split_index = max_length - sentence_end.start()
        else:
            # If no sentence boundary is found, split at last whitespace
            whitespace = text[:max_length].rfind(" ")
            if whitespace != -1:
                split_index = whitespace

        # Append the split part without any delimiter
        parts.append(text[:split_index].strip())
        text = text[split_index:].strip()  # Update text to remaining content

    parts.append(text)  # Add the remaining text as the last part
    return parts


# Function to create a service for the Gmail API
def create_gmail_service():
    # Decode the Base64 string back to JSON and load it
    credentials_info = json.loads(base64.b64decode(SATORIAI_GMAIL_KEY))

    # Create the service account credentials object from the JSON data
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info, scopes=SCOPES
    )

    # Delegating authority to impersonate the user
    delegated_credentials = credentials.with_subject(IMPERSONATED_USER)

    # Building the Gmail API service
    service = build('gmail', 'v1', credentials=delegated_credentials)
    return service


# Function to create the email message
def create_message(sender, to, subject, message_text):
    message = MIMEMultipart('alternative')
    message['to'] = to
    message['from'] = sender
    message['subject'] = subject
    html_part = MIMEText(message_text, 'html')
    message.attach(html_part)
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {'raw': raw_message}


# Function to send the email using Gmail API
def send_email(service, sender, to, subject, body):
    try:
        # Create the email message
        message = create_message(sender, to, subject, body)

        # Send the email
        message = service.users().messages().send(userId='me', body=message).execute()
        print(f'Message Id: {message["id"]}')
    except HttpError as error:
        print(f'An error occurred: {error}')
        message = None


def send_whatsapp(to, body, image_url=None):
    message_params = {
        "body": body,
        "from_": TWILIO_PHONE_NUMBER,  # e.g., 'whatsapp:+14155238886'
        "to": to  # e.g., 'whatsapp:+919999999999'
    }

    if image_url:
        message_params["media_url"] = [image_url]

    response = twilio_client.messages.create(**message_params)
    print(response)


def get_color_list():
    """
    Fetches a list of colors from the 'colors' table in the given SQLite database.

    Returns:
        List[str]: A list of color names.
    """
    try:
        engine = create_engine(DB_URL)
        with engine.connect() as connection:
            result = connection.execute(text("SELECT colour FROM railway.product_info"))
            colors = [row[0] for row in result.fetchall()]
            return ", ".join(colors)
    except Exception as e:
        print(f"Error fetching colors: {e}")
        return []


def get_product_info(fabric, color):
    try:
        output = []
        engine = create_engine(DB_URL)
        query = text("SELECT * FROM railway.product_info WHERE fabric = :fabric AND colour = :color")
        with engine.connect() as connection:
            result = connection.execute(query, {"fabric": fabric, "color": color})
            response = result.fetchall()
            for row in response:
                output.append({"product_id": row[0],
                               "fabric": row[1],
                               "color": row[2],
                               "swatch_image": row[3],
                               "additional_images": row[4].split("; ")})
            return output
    except Exception as e:
        print(f"Error fetching colors: {e}")
        return []

