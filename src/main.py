from fastapi import FastAPI

from routes import whatsapp

app = FastAPI()
app.include_router(whatsapp)
