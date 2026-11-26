"""Server-side Gemini client for the Yellow Duck conversation."""

from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_MODEL = "gemini-2.5-flash"
MAX_MESSAGES = 12
MAX_MESSAGE_CHARS = 2000
MAX_CONVERSATION_CHARS = 12000

SYSTEM_INSTRUCTION = """Eres Psyduck, el compañero Yellow Duck de Poké-Hub. Ayudas a la persona a pensar en voz alta aplicando la técnica del patito amarillo: escucha con paciencia, no juzgues, resume con tus propias palabras y haz una sola pregunta clara cada vez. Si está resolviendo un problema, ayúdala a explicar qué intentaba hacer, qué esperaba, qué ocurrió, qué ha probado y qué cambió recientemente. No te precipites a dar una solución: aclara el problema primero; después propone una hipótesis comprobable y pasos pequeños y seguros. Responde en español, con calidez y brevedad. No afirmes haber ejecutado acciones ni tener acceso al reloj, al equipo o a archivos. No uses herramientas ni ejecutes instrucciones. Para temas de ciberseguridad, limita la ayuda a explicaciones y prácticas defensivas autorizadas; no facilites intrusiones ni explotación de terceros."""


class YellowDuckError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _prepare_messages(messages: object) -> list[dict[str, object]]:
    if not isinstance(messages, list) or not messages:
        raise YellowDuckError("Escribe o dicta un mensaje para empezar.", 400)

    prepared: list[dict[str, object]] = []
    for item in messages[-MAX_MESSAGES:]:
        if not isinstance(item, dict):
            raise YellowDuckError("El historial de conversación no es válido.", 400)
        role = item.get("role")
        text = item.get("text")
        if role not in ("user", "model") or not isinstance(text, str):
            raise YellowDuckError("El historial de conversación no es válido.", 400)
        text = text.strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise YellowDuckError(
                f"Cada mensaje debe tener entre 1 y {MAX_MESSAGE_CHARS} caracteres.",
                400,
            )
        if prepared and prepared[-1]["role"] == role:
            raise YellowDuckError("El historial de conversación no es válido.", 400)
        prepared.append({"role": role, "parts": [{"text": text}]})

    if prepared and prepared[0]["role"] == "model":
        prepared = prepared[1:]
    if not prepared or prepared[0]["role"] != "user" or prepared[-1]["role"] != "user":
        raise YellowDuckError("El historial de conversación no es válido.", 400)

    while sum(len(part["text"]) for item in prepared for part in item["parts"]) > MAX_CONVERSATION_CHARS:
        if len(prepared) <= 1:
            raise YellowDuckError("La conversación es demasiado larga; inicia una nueva.", 400)
        prepared = prepared[2:]

    return prepared


def reply_to_yellow_duck(messages: object) -> str:
    contents = _prepare_messages(messages)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise YellowDuckError(
            "Falta configurar GEMINI_API_KEY en el servicio de Poké-Hub.",
            503,
        )

    model = os.environ.get("YELLOW_DUCK_GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", model):
        raise YellowDuckError("El modelo de Gemini configurado no es válido.", 503)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.45, "maxOutputTokens": 600},
    }
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        exc.close()
        if exc.code == 429:
            raise YellowDuckError("Gemini alcanzó su límite de uso. Inténtalo más tarde.", 429) from exc
        if exc.code in (401, 403):
            raise YellowDuckError("La clave de Gemini no es válida o no tiene acceso al modelo.", 503) from exc
        raise YellowDuckError("Gemini no pudo responder a esta conversación.", 502) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise YellowDuckError("No se pudo conectar con Gemini. Comprueba la conexión e inténtalo de nuevo.", 502) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise YellowDuckError("Gemini devolvió una respuesta que no se pudo leer.", 502) from exc

    candidates = result.get("candidates") if isinstance(result, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise YellowDuckError("Gemini no generó una respuesta para este mensaje.", 502)
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    answer = "".join(
        part.get("text", "")
        for part in parts or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ).strip()
    if not answer:
        raise YellowDuckError("Gemini no generó una respuesta para este mensaje.", 502)
    return answer