import asyncio
import json
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

import config
from api.deps import get_engine
from api.schemas import (
    EventInfo,
    GameStateResponse,
    MessageResponse,
    NarrativeResponse,
    SaveListResponse,
)
from api.tts import SentenceBuffer, encode_audio_event, synthesize_and_enqueue
from runtime.game import GameEngine

router = APIRouter(prefix="/api/game", tags=["game"])


class ActionRequest(BaseModel):
    action: str = Field(..., min_length=1)


class LoadRequest(BaseModel):
    slot: int


class SaveRequest(BaseModel):
    slot: int
    description: str = ""


def _state_event(engine: GameEngine, *, use_live: bool = False) -> str:
    """Build state event JSON.

    use_live=True reads engine's mutable fields directly (for mid-stream state),
    use_live=False reads the frozen response_state snapshot (for end-of-stream).
    """
    if use_live:
        eid = engine.current_event_id
        event = engine.world.get_event(eid) if eid else None
        return json.dumps({
            "phase": engine.current_phase.value if engine.current_phase else None,
            "eventId": eid,
            "turn": engine.total_turns,
            "awaitingNextEvent": engine.awaiting_next_event,
            "gameEnded": engine.game_ended,
            "eventHasImage": bool(event and event.image),
        }, ensure_ascii=False)
    snap = engine.response_state
    event = engine.world.get_event(snap.event_id) if snap.event_id else None
    return json.dumps({
        "phase": snap.phase,
        "eventId": snap.event_id,
        "turn": snap.turn,
        "awaitingNextEvent": snap.awaiting_next_event,
        "gameEnded": snap.game_ended,
        "eventHasImage": bool(event and event.image),
    }, ensure_ascii=False)


async def _stream_with_tts(
    engine: GameEngine,
    run_fn: Callable[[Callable[[str], None]], None],
    tts: bool,
    voice: str,
    *,
    setup: Callable[[Callable[[str], None]], None] | None = None,
    teardown: Callable[[], None] | None = None,
):
    """Shared SSE generator. Yields chunk events and optionally audio events.
    共享的 SSE 生成器。会产出文本分片事件，并在需要时产出音频事件。

    Args:
        engine: GameEngine instance.
            engine：GameEngine 实例。
        run_fn: Blocking function that takes on_chunk callback.
            run_fn：阻塞函数，接收 on_chunk 回调。
        tts: Whether to enable TTS audio events.
            tts：是否启用 TTS 音频事件。
        voice: TTS voice name.
            voice：TTS 音色名称。
        setup: Optional pre-run setup (e.g. set engine.on_narrative_chunk).
            setup：可选的预运行设置（例如设置 engine.on_narrative_chunk）。
        teardown: Optional post-run cleanup.
            teardown：可选的运行后清理。
    """
    # 事件循环与跨线程文本桥：后台线程通过 on_chunk 推送文本到异步队列
    loop = asyncio.get_event_loop()
    chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()

    def on_chunk(text: str) -> None:
        loop.call_soon_threadsafe(chunk_queue.put_nowait, text)

    # 可选前置注入（例如把 engine 的分片回调指向 on_chunk）
    if setup:
        setup(on_chunk)

    # 把阻塞式 run_fn 放到线程池执行，避免阻塞当前 async 请求
    future = loop.run_in_executor(None, run_fn, on_chunk)

    # TTS 相关运行态：分句缓冲、音频结果队列、任务列表
    sentence_buf = SentenceBuffer() if tts else None
    audio_queue: asyncio.Queue[tuple[int, bytes] | None] | None = (
        asyncio.Queue() if tts else None
    )
    tts_tasks: list[asyncio.Task] = []
    sentence_idx = 0
    saw_text = False
    # Buffer for reordering: hold out-of-order audio until the expected index arrives
    next_audio_idx = 0
    audio_hold: dict[int, bytes] = {}

    # 把新分出的句子异步提交给 TTS，并记录句子序号用于后续有序输出
    def _dispatch_sentences(sentences: list[str]) -> None:
        nonlocal sentence_idx
        for s in sentences:
            task = asyncio.create_task(
                synthesize_and_enqueue(s, sentence_idx, audio_queue, voice)  # type: ignore[arg-type]
            )
            tts_tasks.append(task)
            sentence_idx += 1

    def _drain_ordered_audio():
        """Yield audio events in strict index order."""
        nonlocal next_audio_idx
        results = []
        if not audio_queue:
            return results
        while not audio_queue.empty():
            item = audio_queue.get_nowait()
            if item:
                audio_hold[item[0]] = item[1]
        while next_audio_idx in audio_hold:
            results.append(encode_audio_event(next_audio_idx, audio_hold.pop(next_audio_idx)))
            next_audio_idx += 1
        return results

    # --- Stream text chunks ---
    # 主循环：持续消费 chunk_queue，并实时向前端推送 SSE chunk/state/audio
    early_state_sent = False
    while not future.done():
        try:
            chunk = await asyncio.wait_for(chunk_queue.get(), timeout=0.1)
            if not early_state_sent:
                early_state_sent = True
                yield f"event: state\ndata: {_state_event(engine, use_live=True)}\n\n"
            data = json.dumps({"text": chunk}, ensure_ascii=False)
            saw_text = True
            yield f"event: chunk\ndata: {data}\n\n"
            if sentence_buf:
                _dispatch_sentences(sentence_buf.append(chunk))
        except asyncio.TimeoutError:
            pass

        for evt in _drain_ordered_audio():
            yield evt

    # --- Check for errors ---
    # 后台任务结束后检查异常；若失败，发 error + done 并终止流
    try:
        result = await future
    except Exception as e:
        err = json.dumps({"message": str(e)}, ensure_ascii=False)
        yield f"event: error\ndata: {err}\n\n"
        yield f"event: done\ndata: {{}}\n\n"
        return

    # --- Drain remaining text chunks ---
    while not chunk_queue.empty():
        chunk = chunk_queue.get_nowait()
        data = json.dumps({"text": chunk}, ensure_ascii=False)
        saw_text = True
        yield f"event: chunk\ndata: {data}\n\n"
        if sentence_buf:
            _dispatch_sentences(sentence_buf.append(chunk))

    # --- Flush sentence buffer ---
    # 把最后未凑成完整句的尾部文本也送去 TTS，并等待所有 TTS 任务完成
    if sentence_buf:
        remaining = sentence_buf.flush()
        if remaining:
            task = asyncio.create_task(
                synthesize_and_enqueue(remaining, sentence_idx, audio_queue, voice)  # type: ignore[arg-type]
            )
            tts_tasks.append(task)

        if tts_tasks:
            await asyncio.gather(*tts_tasks, return_exceptions=True)
        for evt in _drain_ordered_audio():
            yield evt

    # 可选后置清理（例如恢复 engine 回调），最后发送冻结态 state 与 done
    if teardown:
        teardown()

    yield f"event: state\ndata: {_state_event(engine)}\n\n"
    yield f"event: done\ndata: {{}}\n\n"


def _apply_language(lang: str, voice: str) -> str:
    """Set output language and resolve TTS voice."""
    config.OUTPUT_LANGUAGE = lang
    return voice if voice != "auto" else config.get_default_tts_voice()


@router.post("/start")
async def start_game(
    tts: bool = Query(False),
    voice: str = Query("auto"),
    lang: str = Query("zh-CN"),
    engine: GameEngine = Depends(get_engine),
):
    resolved_voice = _apply_language(lang, voice)
    stream = _stream_with_tts(engine, engine.new_game, tts, resolved_voice)
    return StreamingResponse(stream, media_type="text/event-stream")


@router.post("/action")
async def player_action(
    request: ActionRequest,
    tts: bool = Query(False),
    voice: str = Query("auto"),
    lang: str = Query("zh-CN"),
    engine: GameEngine = Depends(get_engine),
):
    resolved_voice = _apply_language(lang, voice)

    def run_fn(on_chunk: Callable[[str], None]) -> None:
        engine.process_input(request.action)

    stream = _stream_with_tts(
        engine,
        run_fn,
        tts,
        resolved_voice,
        setup=lambda cb: setattr(engine, "on_narrative_chunk", cb),
        teardown=lambda: setattr(engine, "on_narrative_chunk", None),
    )
    return StreamingResponse(stream, media_type="text/event-stream")


@router.post("/continue")
async def continue_game(
    tts: bool = Query(False),
    voice: str = Query("auto"),
    lang: str = Query("zh-CN"),
    engine: GameEngine = Depends(get_engine),
):
    resolved_voice = _apply_language(lang, voice)
    stream = _stream_with_tts(engine, engine.continue_game, tts, resolved_voice)
    return StreamingResponse(stream, media_type="text/event-stream")


@router.get("/state", response_model=GameStateResponse)
async def game_state(engine: GameEngine = Depends(get_engine)):
    snap = engine.response_state
    event_info = None
    if snap.event_id:
        event = engine.world.get_event(snap.event_id)
        if event:
            event_info = EventInfo(
                id=event.id,
                decision_text=event.decision_text,
                goal=event.goal,
                importance=event.importance.value,
                type=event.type,
                has_image=bool(event.image),
            )

    return GameStateResponse(
        phase=snap.phase,
        event=event_info,
        turn=snap.turn,
        player_name=engine.player_name or None,
        awaiting_next_event=snap.awaiting_next_event,
        game_ended=snap.game_ended,
    )


@router.post("/save", response_model=MessageResponse)
async def save_game(
    request: SaveRequest,
    engine: GameEngine = Depends(get_engine),
):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, engine.save_game, request.slot, request.description
    )
    return MessageResponse(message=result)


@router.get("/saves", response_model=SaveListResponse)
async def list_saves(engine: GameEngine = Depends(get_engine)):
    return SaveListResponse(saves=engine.list_saves())


@router.post("/load", response_model=NarrativeResponse)
async def load_game(
    request: LoadRequest,
    engine: GameEngine = Depends(get_engine),
):
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, engine.load_game, request.slot)
    snap = engine.response_state
    return NarrativeResponse(
        text=text,
        phase=snap.phase,
        event_id=snap.event_id,
        turn=snap.turn,
    )


@router.get("/event-image/{event_id}")
async def get_event_image(
    event_id: str,
    engine: GameEngine = Depends(get_engine),
):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, engine.get_event_image, event_id)
    if not result:
        raise HTTPException(404, "No image for this event")
    data, media_type = result
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "max-age=3600"})
