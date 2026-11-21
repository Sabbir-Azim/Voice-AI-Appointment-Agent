"""Medical clinic receptionist voice agent backed by MongoDB Atlas.

Built on the LiveKit + MongoDB starter's five integration patterns:

1. RAG with $vectorSearch              -> search_knowledge tool
2. Agentic memory tools                -> remember/recall/forget/search_memories
3. Identify + pre-load context         -> preload_user (entrypoint)
4. Function-tool CRUD                  -> appointment tools (tools/appointments.py)
5. Session report persistence          -> on_session_end
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    ChatContext,
    JobContext,
    RunContext,
    ToolError,
    TurnHandlingOptions,
    cli,
    function_tool,
    inference,
    room_io,
)
from livekit.agents.worker import ServerEnvOption
from livekit.plugins import ai_coustics
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from db.client import aclose, get_db
from tools import appointments
from tools.appointments import AppointmentError
from tools.embeddings import embed_text
from tools.memory import forget, list_memories, recall, remember, search_memory

load_dotenv(".env.local")

logger = logging.getLogger("agent")

# Clinic settings. Doctor hours in the `doctors` collection are interpreted in
# CLINIC_TIMEZONE, and every date/time the LLM sees or sends is clinic-local.
CLINIC_NAME = os.getenv("CLINIC_NAME") or "Sunrise Clinic"
CLINIC_TZ = ZoneInfo(os.getenv("CLINIC_TIMEZONE") or "America/New_York")

# Fallback identity used only when ctx.job.metadata is absent, e.g. when
# running `uv run src/agent.py console`. The frontend always provides a
# real per-browser user_id via agent dispatch metadata.
DEFAULT_USER_ID = "user_1"
DEFAULT_TENANT_ID = "default"

# Allow-list for identity fields that `update_profile` can write to the
# `users` document. Values are dotted paths so nested preferences work
# without a second tool. Anything outside this map belongs in `memories`.
_PROFILE_PATHS = {
    "name": "name",
    "email": "email",
    "phone": "phone",
    "date_of_birth": "date_of_birth",
    "preferred_language": "preferences.language",
    "timezone": "preferences.timezone",
}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def receptionist_instructions(now: datetime) -> str:
    """System prompt for the receptionist, anchored to the current clinic date."""
    local_now = now.astimezone(CLINIC_TZ)
    return (
        f"You are the friendly front-desk receptionist at {CLINIC_NAME}, a "
        "medical clinic. You are speaking on a voice call, so reply in plain "
        "text without markdown, lists, or emojis, and keep each reply to one "
        "or two short sentences. "
        f"Today is {local_now:%A, %B} {local_now.day}, {local_now.year} and the "
        f"clinic's local time is {local_now:%H:%M}. Work out relative dates "
        "like 'next Tuesday' yourself and pass dates to tools as YYYY-MM-DD "
        "and times as YYYY-MM-DDTHH:MM in clinic time. "
        "Booking: ask what the visit is for and whether they want a specific "
        "doctor; if not, call list_doctors and suggest one whose specialty "
        "fits. Call check_availability and offer two or three of the returned "
        "times using their spoken form. Never invent a time. Make sure the "
        "patient's full name is on file, then read back the doctor, day, and "
        "time and wait for a clear yes before calling book_appointment. "
        "Rescheduling: call my_appointments, book the new time first, then "
        "cancel the old one. Cancelling: call my_appointments, confirm which "
        "appointment, then call cancel_appointment. Never read ids aloud. "
        "For questions about hours, location, parking, insurance, fees, "
        "policies, refills, results, or what to bring, call search_knowledge "
        "and answer only from its results. "
        "When the patient tells you their name, email, phone number, or date "
        "of birth, call update_profile. Use remember_detail for lasting facts "
        "such as allergies, accessibility needs, or a preferred doctor. "
        "You are not a clinician: never diagnose, give medical advice, or "
        "interpret symptoms or test results; offer to book a visit instead. "
        "If the caller describes an emergency such as chest pain, trouble "
        "breathing, or severe bleeding, tell them to hang up and call 911 now."
    )


async def _vector_search_knowledge(
    db: AsyncDatabase, query: str, limit: int = 3
) -> list[dict]:
    """Run the shared knowledge vector search and return {title, content} docs."""
    query_embedding = await embed_text(query, input_type="query")
    pipeline = [
        {
            "$vectorSearch": {
                "index": "knowledge_embedding_index",
                "path": "embedding",
                "queryVector": query_embedding,
                "numCandidates": 100,
                "limit": limit,
            }
        },
        {"$project": {"title": 1, "content": 1, "_id": 0}},
    ]
    cursor = await db.knowledge.aggregate(pipeline)
    return await cursor.to_list(length=limit)


class MongoAgent(Agent):
    """Voice agent that wires MongoDB Atlas into the LiveKit pipeline."""

    def __init__(
        self, *, chat_ctx: ChatContext, user_id: str, tenant_id: str
    ) -> None:
        super().__init__(
            chat_ctx=chat_ctx,
            instructions=receptionist_instructions(_now()),
        )
        self._user_id = user_id
        self._tenant_id = tenant_id

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions=(
                f"Greet the caller as the receptionist at {CLINIC_NAME}. If "
                "the loaded profile has a name, greet them by name. If no "
                "name is on file, ask for their full name and call "
                "update_profile with field='name' when they tell you. Then "
                "ask how you can help today."
            )
        )

    @function_tool()
    async def list_doctors(self, context: RunContext) -> str:
        """List the clinic's doctors with their specialty, days, and hours."""
        db = await get_db()
        return json.dumps({"doctors": await appointments.list_doctors(db)})

    @function_tool()
    async def check_availability(
        self, context: RunContext, doctor_id: str, date: str
    ) -> str:
        """Find open appointment times for a doctor.

        Args:
            doctor_id: The doctor's id from list_doctors, e.g. 'dr_khan'.
            date: Preferred day as YYYY-MM-DD in clinic time. If that day is
                full or the doctor is off, the next day with openings is
                returned and requested_date_available is false.
        """
        db = await get_db()
        try:
            result = await appointments.check_availability(
                db, doctor_id, date, CLINIC_TZ
            )
        except AppointmentError as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps(result)

    @function_tool()
    async def book_appointment(
        self, context: RunContext, doctor_id: str, start: str, reason: str
    ) -> str:
        """Book an appointment after the patient has confirmed the details.

        Args:
            doctor_id: The doctor's id from list_doctors.
            start: Slot start from check_availability, as YYYY-MM-DDTHH:MM.
            reason: Short reason for the visit in the patient's words.
        """
        context.disallow_interruptions()
        db = await get_db()
        try:
            result = await appointments.book_appointment(
                db, self._user_id, doctor_id, start, reason, CLINIC_TZ
            )
        except AppointmentError as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps(result)

    @function_tool()
    async def my_appointments(self, context: RunContext) -> str:
        """List the patient's upcoming appointments, soonest first."""
        db = await get_db()
        results = await appointments.list_appointments(db, self._user_id, CLINIC_TZ)
        return json.dumps({"appointments": results})

    @function_tool()
    async def cancel_appointment(
        self, context: RunContext, appointment_id: str
    ) -> str:
        """Cancel one of the patient's appointments after they confirm.

        Args:
            appointment_id: The appointment_id from my_appointments.
        """
        context.disallow_interruptions()
        db = await get_db()
        try:
            return await appointments.cancel_appointment(
                db, self._user_id, appointment_id
            )
        except AppointmentError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def search_knowledge(
        self, context: RunContext, query: str
    ) -> str:
        """Search the clinic's FAQ for facts the caller asks about.

        Use for opening hours, address, parking, accepted insurance, fees,
        cancellation policy, what to bring, refills, and test results.
        Returns a JSON object with a `results` array of `{title, content}`.
        """

        async def _speak_status_update(delay: float = 0.5) -> None:
            await asyncio.sleep(delay)
            await context.session.generate_reply(
                instructions=(
                    f"You are looking up '{query}' in the clinic's FAQ "
                    "but it is taking a moment. Give the user a brief, "
                    "one-sentence update that you are looking it up."
                )
            )

        status_task = asyncio.create_task(_speak_status_update(0.5))
        try:
            db = await get_db()
            results = await _vector_search_knowledge(db, query, limit=3)
        finally:
            status_task.cancel()
        return json.dumps({"results": results})

    @function_tool()
    async def update_profile(
        self, context: RunContext, field: str, value: str
    ) -> str:
        """Update an identity field on the user's profile.

        Use for name, email, phone, date_of_birth, preferred_language, or
        timezone. These are
        first-class profile fields stored on the `users` document and
        loaded at session start. For anything else, use remember_detail.
        """
        if field not in _PROFILE_PATHS:
            raise ToolError(
                f"Unknown profile field '{field}'. "
                f"Allowed: {sorted(_PROFILE_PATHS)}"
            )
        db = await get_db()
        now = _now()
        await db.users.update_one(
            {"user_id": self._user_id},
            {
                "$set": {_PROFILE_PATHS[field]: value, "updated_at": now},
                "$setOnInsert": {
                    "user_id": self._user_id,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return f"Updated {field} to {value}."

    @function_tool()
    async def remember_detail(
        self, context: RunContext, memory_type: str, content: str
    ) -> str:
        """Store or replace a fact under memory_type.

        Use for allergies, accessibility needs, a preferred doctor, or
        other lasting facts the patient volunteers. Pick a short specific
        label like 'allergy' or 'preferred_doctor' rather than a generic one.
        """
        db = await get_db()
        return await remember(
            db, self._user_id, self._tenant_id, memory_type, content
        )

    @function_tool()
    async def recall_detail(
        self, context: RunContext, memory_type: str
    ) -> str:
        """Return the value stored under memory_type by exact label.

        Returns 'No memory found' when the label is not set. Prefer
        search_memories when you are unsure which label stores the fact.
        """
        db = await get_db()
        return await recall(db, self._user_id, self._tenant_id, memory_type)

    @function_tool()
    async def forget_detail(
        self, context: RunContext, memory_type: str
    ) -> str:
        """Delete the value stored under memory_type."""
        db = await get_db()
        return await forget(db, self._user_id, self._tenant_id, memory_type)

    @function_tool()
    async def search_memories(self, context: RunContext, query: str) -> str:
        """Find memories by meaning using hybrid vector and text search.

        Use when you do not know the exact label or when pulling
        background context. Returns a list of {memory_type, content}
        objects so you can follow up with recall_detail or forget_detail.
        """
        db = await get_db()
        results = await search_memory(
            db, self._user_id, self._tenant_id, query, limit=3
        )
        return json.dumps({"results": results})

    @function_tool()
    async def list_user_memories(self, context: RunContext) -> str:
        """Return every slot stored for this user, newest first."""
        db = await get_db()
        results = await list_memories(db, self._user_id, self._tenant_id)
        return json.dumps(
            {
                "results": [
                    {"memory_type": r["memory_type"], "content": r["content"]}
                    for r in results
                ]
            }
        )


async def preload_user(user_id: str, tenant_id: str) -> ChatContext:
    """Pattern 3: load user data into the chat context before the session.

    Upserts the `users` row so every connected user (including anonymous
    cookie visitors) has a stable profile document, then appends any
    memory slots the agent has learned for this (user_id, tenant_id) in
    prior sessions. Both writes land as assistant messages so the LLM
    sees them before the first reply.
    """
    db = await get_db()
    now = _now()
    user = await db.users.find_one_and_update(
        {"user_id": user_id},
        {
            "$set": {"last_seen_at": now},
            "$setOnInsert": {"user_id": user_id, "created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    chat_ctx = ChatContext()
    name = user.get("name")
    email = user.get("email")
    prefs = user.get("preferences", {})
    if name or email or prefs:
        chat_ctx.add_message(
            role="assistant",
            content=(
                f"User profile: name={name or 'unknown'}, "
                f"email={email or 'unknown'}, preferences={prefs}."
            ),
        )
    else:
        chat_ctx.add_message(
            role="assistant",
            content=(
                f"No stored profile fields yet for user_id {user_id}. "
                "Greet them as a new user, then ask for their name and "
                "call update_profile with field='name' so it persists."
            ),
        )
    if not name:
        chat_ctx.add_message(
            role="assistant",
            content=(
                "No name on file for this user. Ask them for their name "
                "and call update_profile with field='name' to save it."
            ),
        )

    memories = await list_memories(db, user_id, tenant_id)
    if memories:
        lines = "\n".join(
            f"- {m['memory_type']}: {m['content']}" for m in memories
        )
        chat_ctx.add_message(
            role="assistant",
            content=f"Remembered facts from prior sessions:\n{lines}",
        )
    return chat_ctx


async def on_session_end(ctx: JobContext) -> None:
    """Pattern 5: persist a session report to MongoDB on hangup."""
    try:
        report = ctx.make_session_report()
        db = await get_db()
        user_id = ctx.proc.userdata.get("user_id", DEFAULT_USER_ID)
        tenant_id = ctx.proc.userdata.get("tenant_id", DEFAULT_TENANT_ID)
        await db.sessions.insert_one(
            {
                "session_id": ctx.room.name,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "room_name": ctx.room.name,
                "report": report.to_dict(),
            }
        )
        logger.info("Persisted session report for %s", ctx.room.name)
    except Exception:
        logger.exception("Failed to persist session report")
    finally:
        await aclose()


# On Windows the first job process cold-imports the inference stack in ~10-15s,
# longer than the default 10s init timeout and close to the frontend's ~20s wait
# for the agent to join. Dev mode keeps no warm processes by default, so every
# call after a (re)start paid that cost. Keep one warm in dev, leave the
# production default unchanged, and allow a slower first start.
server = AgentServer(
    num_idle_processes=ServerEnvOption(
        dev_default=1,
        prod_default=AgentServer._default_num_idle_processes.prod_default,
    ),
    initialize_process_timeout=30.0,
)


@server.rtc_session(agent_name="my-agent", on_session_end=on_session_end)
async def my_agent(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}

    # Pattern 3 setup: identify the user from agent dispatch metadata.
    # The frontend packs {"user_id", "tenant_id"} into ctx.job.metadata via
    # room_config.agents[0].metadata. Parsing here (before ctx.connect) keeps
    # the preload network call out of the connection critical path. See:
    # https://docs.livekit.io/agents/logic/external-data/
    meta: dict[str, str] = {}
    if ctx.job.metadata:
        try:
            meta = json.loads(ctx.job.metadata)
        except json.JSONDecodeError:
            logger.warning("ctx.job.metadata was not valid JSON; using defaults")

    user_id = meta.get("user_id", DEFAULT_USER_ID)
    tenant_id = meta.get("tenant_id", DEFAULT_TENANT_ID)
    ctx.proc.userdata["user_id"] = user_id
    ctx.proc.userdata["tenant_id"] = tenant_id

    initial_ctx = await preload_user(user_id, tenant_id)

    session = AgentSession(
        stt=inference.STT(model="deepgram/nova-3", language="multi"),
        llm=inference.LLM(model="openai/gpt-5.3-chat-latest"),
        tts=inference.TTS(
            model="cartesia/sonic-3", voice="9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
        ),
        turn_handling=TurnHandlingOptions(
            # LiveKit's audio turn detector: predicts end of turn from the user's
            # audio (semantics plus intonation and pacing). AgentSession supplies
            # the VAD it needs automatically.
            # See https://docs.livekit.io/agents/logic/turns/turn-detector/
            turn_detection=inference.TurnDetector(),
            preemptive_generation={"enabled": True},
        ),
    )

    await session.start(
        agent=MongoAgent(
            chat_ctx=initial_ctx,
            user_id=user_id,
            tenant_id=tenant_id,
        ),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_L
                ),
            ),
        ),
    )

    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
