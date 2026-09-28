from dotenv import load_dotenv

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from db_tools import DATABASE_TOOLS

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()


# model = init_chat_model(
#     model = "gpt-4.1-mini",
#     temperature = 0.1
# )

SYSTEM_PROMPT = """You are an AI assistant for the Goal Post Pro database (athletics personnel, drills, events and scouting appraisals).
Use your tools to answer questions from the data instead of guessing. If unsure of the schema, call describeDatabase first.
Prefer queryRecords / getPersonnelProfile; use runSelectQuery for joins and aggregates.
Before calling createRecord, updateRecord or deleteRecord, tell the user exactly what you are about to change and wait for their confirmation, unless they have already explicitly asked for that exact change.
After a change, report what was changed. If a tool returns an error, explain it plainly or fix the call and retry.
You cannot access user accounts or passwords."""

agent = create_agent(
    model='gpt-4.1-mini',
    tools=DATABASE_TOOLS,
    system_prompt=SYSTEM_PROMPT
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