from fastapi import FastAPI

from app.contractguard.http_api import create_app

app = create_app()


@app.get("/")
def read_root():
    return {"Hello": "World"}
