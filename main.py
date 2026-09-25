from dotenv import load_dotenv
import os
import mysql.connector

from langchain.agents import create_agent
from langchain.tools import tool, ToolRuntime
from langchain.chat_models import init_chat_model

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

mysql_config = {
    "host": os.getenv("MYSQL_HOST"),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE")
}

connection = mysql.connector.connect(**mysql_config)

db_cursor = connection.cursor()

@tool("getAllPersonnel", description="Retrieve all personnel from the database", return_direct=False)
def getAllPersonnel():
    db_cursor.execute("SELECT * FROM personnel")
    results = db_cursor.fetchall()
    personnel_list = []
    for row in results:
        p_id = row[0]
        p_name = f"{row[1]} {row[2]}"
        personnel_list.append({"personnel_id": p_id, "name": p_name})

    # print(f'personnel_list: {personnel_list}')
    return personnel_list


# def query_database(query: str) -> str:
#     db_cursor.execute(query)
#     result = db_cursor.fetchall()
#     return str(result)


# model = init_chat_model(
#     model = "gpt-4.1-mini",
#     temperature = 0.1
# )

agent = create_agent(
    model='gpt-4.1-mini',
    tools=[getAllPersonnel],
    system_prompt="You are an AI assistant that can answer questions and provide information from the goalpost database."
)

conversation_history: list = []

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    global conversation_history
    conversation_history.append({"role": "user", "content": req.message})
    result = agent.invoke({"messages": conversation_history})
    conversation_history = result["messages"]
    reply_text = str(conversation_history[-1].content)
    return ChatResponse(reply=reply_text)