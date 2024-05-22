import argparse
import asyncio
import base64
import dataclasses
import json
import mimetypes
import os
import re
import time
import urllib
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

import aiohttp
import dataclasses_json

TokenGenerator = AsyncGenerator[str, None]
ApiResult = Tuple[aiohttp.ClientResponse, TokenGenerator]

AZURE_OPENAI_API_VERSION = "2024-02-15-preview"
MAX_TTFT = 9.99
MAX_TOTAL_TIME = 99.99


@dataclasses.dataclass
class InputFile:
    @classmethod
    def from_file(cls, path: str):
        mime_type, _ = mimetypes.guess_type(path)
        if not mime_type:
            raise ValueError(f"Unknown file type: {path}")
        with open(path, "rb") as f:
            data = f.read()
        return cls(mime_type, data)

    mime_type: str
    data: bytes

    @property
    def base64_data(self):
        return base64.b64encode(self.data).decode("utf-8")


@dataclasses.dataclass
class ApiMetrics(dataclasses_json.DataClassJsonMixin):
    model: str
    ttr: Optional[float] = None
    ttft: Optional[float] = None
    tps: Optional[float] = None
    input_tokens: Optional[int] = None
    num_tokens: Optional[int] = None
    total_time: Optional[float] = None
    output: Optional[str] = None
    error: Optional[str] = None


@dataclasses.dataclass
class ApiContext:
    session: aiohttp.ClientSession
    index: int
    name: str
    func: Callable
    model: str
    prompt: str
    files: List[InputFile]
    temperature: float
    max_tokens: int
    detail: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    def __init__(self, session, index, name, func, args, prompt, files):
        self.session = session
        self.index = index
        self.name = name
        self.func = func
        self.model = args.model
        self.prompt = prompt
        self.files = files
        self.detail = args.detail
        self.temperature = args.temperature
        self.max_tokens = args.max_tokens
        self.api_key = args.api_key
        self.base_url = args.base_url
        self.metrics = ApiMetrics(model=self.name)

    async def run(self, on_token: Optional[Callable[["ApiContext", str], None]] = None):
        response = None
        try:
            start_time = time.time()
            first_token_time = None
            response, chunk_gen = await self.func(self)
            self.metrics.ttr = time.time() - start_time
            if response.ok:
                if chunk_gen:
                    self.metrics.num_tokens = 0
                    self.metrics.output = ""
                    async for chunk in chunk_gen:
                        self.metrics.output += chunk
                        self.metrics.num_tokens += 1
                        if not first_token_time:
                            first_token_time = time.time()
                            self.metrics.ttft = first_token_time - start_time
                        if on_token:
                            on_token(self, chunk)
            else:
                self.metrics.error = f"{response.status} {response.reason}"
        except TimeoutError:
            self.metrics.error = "Timeout"
        end_time = time.time()
        if self.metrics.num_tokens:
            token_time = end_time - first_token_time
            self.metrics.total_time = end_time - start_time
            self.metrics.tps = min((self.metrics.num_tokens - 1) / token_time, 999)
        elif self.metrics.error:
            self.metrics.ttft = MAX_TTFT
            self.metrics.tps = 0.0
            self.metrics.total_time = MAX_TOTAL_TIME
        if response:
            await response.release()


async def post(
    ctx: ApiContext,
    url: str,
    headers: dict,
    data: dict,
    make_chunk_gen: Optional[Callable[[aiohttp.ClientResponse], TokenGenerator]] = None,
):
    response = await ctx.session.post(url, headers=headers, data=json.dumps(data))
    chunk_gen = make_chunk_gen(response) if make_chunk_gen else None
    return response, chunk_gen


def get_api_key(ctx: ApiContext, env_var: str) -> str:
    if ctx.api_key:
        return ctx.api_key
    if env_var in os.environ:
        return os.environ[env_var]
    raise ValueError(f"Missing API key: {env_var}")


def make_headers(
    snowflake_use_jwt: Optional[bool] = None,
    auth_token: Optional[str] = None,
    api_key: Optional[str] = None,
    x_api_key: Optional[str] = None,
):
    headers = {
        "content-type": "application/json",
    }
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    if api_key:
        headers["api-key"] = api_key
    if x_api_key:
        headers["x-api-key"] = x_api_key
    return headers


def make_openai_url_and_headers(ctx: ApiContext, path: str):
    url = ctx.base_url or "https://api.openai.com/v1"
    hostname = urllib.parse.urlparse(url).hostname
    use_azure_openai = hostname and hostname.endswith("openai.azure.com")
    if use_azure_openai:
        api_key = get_api_key(ctx, "AZURE_OPENAI_API_KEY")
        headers = make_headers(api_key=api_key)
        url += f"/openai/deployments/{ctx.model.replace('.', '')}{path}?api-version={AZURE_OPENAI_API_VERSION}"
    else:
        api_key = get_api_key(ctx, "OPENAI_API_KEY")
        headers = make_headers(auth_token=api_key)
        url += path
    return url, headers


def make_openai_messages(ctx: ApiContext):
    if not ctx.files:
        return [{"role": "user", "content": ctx.prompt}]

    content: List[Dict[str, Any]] = [{"type": "text", "text": ctx.prompt}]
    for file in ctx.files:
        if not file.mime_type.startswith("image/"):
            raise ValueError(f"Unsupported file type: {file.mime_type}")
        url = f"data:{file.mime_type};base64,{file.base64_data}"
        image_url = {"url": url}
        if ctx.detail:
            image_url["detail"] = ctx.detail
        content.append({"type": "image_url", "image_url": image_url})
    return [{"role": "user", "content": content}]


def make_openai_chat_body(ctx: ApiContext, **kwargs):
    # Models differ in how they want to receive the prompt, so
    # we let the caller specify the key and format.
    body = {
        "model": ctx.model,
        "max_tokens": ctx.max_tokens,
        "temperature": ctx.temperature,
        "stream": True,
    }
    for key, value in kwargs.items():
        body[key] = value
    return body


async def make_sse_chunk_gen(response) -> AsyncGenerator[Dict[str, Any], None]:
    async for line in response.content:
        line = line.decode("utf-8").strip()
        if line.startswith("data:"):
            content = line[5:].strip()
            if content == "[DONE]":
                break
            yield json.loads(content)


async def openai_chunk_gen(response) -> TokenGenerator:
    tokens = 0
    async for chunk in make_sse_chunk_gen(response):
        if chunk["choices"]:
            delta_content = chunk["choices"][0]["delta"].get("content")
            if delta_content:
                tokens += 1
                yield delta_content
        usage = chunk.get("usage")
        if usage:
            num_input_tokens = usage.get("prompt_tokens")
            num_output_tokens = usage.get("completion_tokens")
            while tokens < num_output_tokens:
                tokens += 1
                yield ""


async def openai_chat(ctx: ApiContext, path: str = "/chat/completions") -> ApiResult:
    url, headers = make_openai_url_and_headers(ctx, path)
    data = make_openai_chat_body(ctx, messages=make_openai_messages(ctx))
    return await post(ctx, url, headers, data, openai_chunk_gen)


async def openai_embed(ctx: ApiContext) -> ApiResult:
    url, headers = make_openai_url_and_headers(ctx, "/embeddings")
    data = {"model": ctx.model, "input": ctx.prompt}
    return await post(ctx, url, headers, data)


def make_anthropic_messages(prompt: str, files: Optional[List[InputFile]] = None):
    """Formats the prompt as a text chunk and any images as image chunks.
    Note that Anthropic's image protocol is somewhat different from OpenAI's."""
    if not files:
        return [{"role": "user", "content": prompt}]

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for file in files:
        if not file.mime_type.startswith("image/"):
            raise ValueError(f"Unsupported file type: {file.mime_type}")
        source = {
            "type": "base64",
            "media_type": file.mime_type,
            "data": file.base64_data,
        }
        content.append({"type": "image", "source": source})
    return [{"role": "user", "content": content}]


async def anthropic_chat(ctx: ApiContext) -> ApiResult:
    """Make an Anthropic chat completion request. The request protocol is similar to OpenAI's,
    but the response protocol is completely different."""

    async def chunk_gen(response) -> TokenGenerator:
        tokens = 0
        async for chunk in make_sse_chunk_gen(response):
            delta = chunk.get("delta")
            if delta and delta.get("type") == "text_delta":
                tokens += 1
                yield delta["text"]
            usage = chunk.get("usage")
            if usage:
                num_tokens = usage.get("output_tokens")
                while tokens < num_tokens:
                    tokens += 1
                    yield ""

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "content-type": "application/json",
        "x-api-key": get_api_key(ctx, "ANTHROPIC_API_KEY"),
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "messages-2023-12-15",
    }
    data = make_openai_chat_body(ctx, messages=make_anthropic_messages(ctx.prompt, ctx.files))
    return await post(ctx, url, headers, data, chunk_gen)


async def cohere_chat(ctx: ApiContext) -> ApiResult:
    """Make a Cohere chat completion request."""

    async def chunk_gen(response) -> TokenGenerator:
        tokens = 0
        async for line in response.content:
            chunk = json.loads(line)
            if chunk.get("event_type") == "text-generation" and "text" in chunk:
                tokens += 1
                yield chunk["text"]

    url = "https://api.cohere.ai/v1/chat"
    headers = make_headers(auth_token=get_api_key(ctx, "COHERE_API_KEY"))
    data = make_openai_chat_body(ctx, message=ctx.prompt)
    return await post(ctx, url, headers, data, chunk_gen)


async def cloudflare_chat(ctx: ApiContext) -> ApiResult:
    """Make a Cloudflare chat completion request. The protocol is similar to OpenAI's,
    but the URL doesn't follow the same scheme and the response structure is different.
    """

    async def chunk_gen(response) -> TokenGenerator:
        async for chunk in make_sse_chunk_gen(response):
            yield chunk["response"]

    account_id = os.environ["CF_ACCOUNT_ID"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{ctx.model}"
    headers = make_headers(auth_token=get_api_key(ctx, "CF_API_KEY"))
    data = make_openai_chat_body(ctx, messages=make_openai_messages(ctx))
    return await post(ctx, url, headers, data, chunk_gen)


async def make_json_chunk_gen(response) -> AsyncGenerator[Dict[str, Any], None]:
    """Hacky parser for the JSON streaming format used by Google Vertex AI."""
    buf = ""
    async for line in response.content:
        # Eat the first array bracket, we'll do the same for the last one below.
        line = line.decode("utf-8").strip()
        if not buf and line.startswith("["):
            line = line[1:]
        # Split on comma-only lines, otherwise concatenate.
        if line == ",":
            yield json.loads(buf)
            buf = ""
        else:
            buf += line
    yield json.loads(buf[:-1])


def get_google_access_token():
    from google.auth.transport import requests
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        "service_account.json",
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    if not creds.token:
        creds.refresh(requests.Request())
    return creds.token


def make_google_url_and_headers(ctx: ApiContext, method: str):
    region = "us-west1"
    project_id = os.environ["GCP_PROJECT"]
    url = f"https://{region}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{region}/publishers/google/models/{ctx.model}:{method}"
    api_key = ctx.api_key
    if not api_key:
        api_key = get_google_access_token()
    headers = make_headers(auth_token=api_key)
    return url, headers


def make_gemini_messages(prompt: str, files: List[InputFile]):
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    for file in files:
        parts.append({"inline_data": {"mime_type": file.mime_type, "data": file.base64_data}})

    return [{"role": "user", "parts": parts}]


async def gemini_chat(ctx: ApiContext) -> ApiResult:
    async def chunk_gen(response) -> TokenGenerator:
        tokens = 0
        async for chunk in make_json_chunk_gen(response):
            content = chunk["candidates"][0].get("content")
            if content and "parts" in content:
                part = content["parts"][0]
                if "text" in part:
                    tokens += 1
                    yield part["text"]
            usage = chunk.get("usageMetadata")
            if usage:
                num_tokens = usage.get("candidatesTokenCount")
                while tokens < num_tokens:
                    tokens += 1
                    yield ""

    # The Google AI Gemini API (URL below) doesn't return the number of generated tokens.
    # Instead we use the Google Cloud Vertex AI Gemini API, which does return the number of tokens, but requires an Oauth credential.
    # Also, setting safetySettings to BLOCK_NONE is not supported in the Vertex AI Gemini API, at least for now.
    if True:
        url, headers = make_google_url_and_headers(ctx, "streamGenerateContent")
    else:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{ctx.model}:streamGenerateContent?key={get_api_key(ctx, 'GOOGLE_GEMINI_API_KEY')}"
        headers = make_headers()
    harm_categories = [
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    ]
    data = {
        "contents": make_gemini_messages(ctx.prompt, ctx.files),
        "generationConfig": {
            "temperature": ctx.temperature,
            "maxOutputTokens": ctx.max_tokens,
        },
        "safetySettings": [{"category": category, "threshold": "BLOCK_NONE"} for category in harm_categories],
    }
    return await post(ctx, url, headers, data, chunk_gen)


async def cohere_embed(ctx: ApiContext) -> ApiResult:
    url = "https://api.cohere.ai/v1/embed"
    headers = make_headers(auth_token=get_api_key(ctx, "COHERE_API_KEY"))
    data = {
        "model": ctx.model,
        "texts": [ctx.prompt],
        "input_type": "search_query",
    }
    return await post(ctx, url, headers, data)


async def make_fixie_chunk_gen(response) -> TokenGenerator:
    text = ""
    async for line in response.content:
        line = line.decode("utf-8").strip()
        obj = json.loads(line)
        curr_turn = obj["turns"][-1]
        if curr_turn["role"] == "assistant" and curr_turn["messages"] and "content" in curr_turn["messages"][-1]:
            if curr_turn["state"] == "done":
                break
            new_text = curr_turn["messages"][-1]["content"]
            # Sometimes we get a spurious " " message
            if new_text == " ":
                continue
            if new_text.startswith(text):
                delta = new_text[len(text) :]
                text = new_text
                yield delta
            else:
                print(f"Warning: got unexpected text: '{new_text}' vs '{text}'")


async def fixie_chat(ctx: ApiContext) -> ApiResult:
    url = f"https://api.fixie.ai/api/v1/agents/{ctx.model}/conversations"
    headers = make_headers(auth_token=get_api_key(ctx, "FIXIE_API_KEY"))
    data = {"message": ctx.prompt, "runtimeParameters": {}}
    return await post(ctx, url, headers, data, make_fixie_chunk_gen)


async def make_snowflake_chunk_gen(response) -> TokenGenerator:
    async for line in response.content:

        if line.startswith(b"data:"):
            data = json.loads(line[len("data:") :].strip())
            content = data.get("choices", [{}])[0].get("delta", {}).get("content")
            if content:
                yield content


async def snowflake_chat(ctx: ApiContext) -> ApiResult:
    account = os.getenv("SNOWFLAKE_ACCOUNT")
    url = f"https://{account}.snowflakecomputing.com/api/v2/cortex/inference/complete"
    headers = make_headers(auth_token=get_api_key(ctx, "SNOWFLAKE_AUTH_TOKEN"))
    headers["X-Snowflake-Authorization-Token-Type"] = "KEYPAIR_JWT"
    model = ctx.model
    model_name_replacement = {
        "llama-3-8b-chat": "llama3-8b",
        "llama-3-70b-chat": "llama3-70b",
        "mixtral-8x7b-instruct": "mixtral-8x7b",
    }
    replacement = model_name_replacement[model]
    if replacement is not None:
        model = replacement
    data = {
        "model": model,
        "messages": [{"content": ctx.prompt}],
        "stream": True,
    }
    return await post(ctx, url, headers, data, make_snowflake_chunk_gen)


async def fake_chat(ctx: ApiContext) -> ApiResult:
    class FakeResponse(aiohttp.ClientResponse):
        def __init__(self, status, reason):
            self.status = status
            self.reason = reason

        # async def release(self):
        # pass

    async def make_fake_chunk_gen(output: str):
        for word in output.split():
            yield word + " "
            await asyncio.sleep(0.05)

    output = "This is a fake response."
    if ctx.index % 2 == 0:
        response = FakeResponse(200, "OK")
    else:
        response = FakeResponse(500, "Internal Server Error")
    sleep = 0.5 * (ctx.index + 1)
    max_sleep = ctx.session.timeout.total
    if max_sleep:
        await asyncio.sleep(min(sleep, max_sleep))
    if sleep > max_sleep:
        raise TimeoutError
    return (response, make_fake_chunk_gen(output))


def make_display_name(provider_or_url: str, model: str) -> str:
    # Clean up the base URL to get a nicer provider name.
    if provider_or_url.startswith("https://"):
        provider = (
            provider_or_url[8:]
            .split("/")[0]
            .replace("openai-sub-with-gpt4", "eastus2")
            .replace("fixie-", "")
            .replace("-serverless", "")
            .replace("inference.ai.azure.com", "azure")
            .replace("openai.azure.com", "azure")
        )
        # Get the last two segments of the domain, and swap foo.azure to azure.foo.
        provider = ".".join(provider.split(".")[-2:])
        provider = re.sub(r"(\w+)\.azure$", r"azure.\1", provider)
    else:
        provider = provider_or_url
    model_segments = model.split("/")
    if provider:
        # We already have a provider, so just need to add the model name.
        # If we've got a model name, add the end of the split to the provider.
        # Otherwise, we have model.domain.com, so we need to swap to domain.com/model.
        if model:
            name = provider + "/" + model_segments[-1]
        else:
            domain_segments = provider.split(".")
            name = ".".join(domain_segments[1:]) + "/" + domain_segments[0]
    elif len(model_segments) > 1:
        # We've got a provider/model string, from which we need to get the provider and model.
        provider = model_segments[0]
        name = provider + "/" + model_segments[-1]
    return name


def make_context(
    session: aiohttp.ClientSession,
    index: int,
    args: argparse.Namespace,
    prompt: Optional[str] = None,
    files: Optional[List[InputFile]] = None,
) -> ApiContext:
    model = args.model
    prefix = re.split("-|/", model)[0]
    provider = args.base_url
    match (prefix):
        case "claude":
            provider = "anthropic"
            func = anthropic_chat
        case "command":
            provider = "cohere"
            func = cohere_chat
        case "@cf":
            provider = "cloudflare"
            func = cloudflare_chat
        case "gemini":
            provider = "google"
            func = gemini_chat
        case "text-embedding-ada":
            provider = "openai"
            func = openai_embed
        case "embed":
            provider = "cohere"
            func = cohere_embed
        case "fake":
            provider = "test"
            func = fake_chat
        case "llama":
            provider = "snowflake"
            func = snowflake_chat
        case "mixtral":
            provider = "snowflake"
            func = snowflake_chat
        case _ if args.base_url or model.startswith("gpt-") or model.startswith("ft:gpt-"):
            func = openai_chat
            if not args.base_url:
                provider = "openai"
        # case _ elif "/" in model return await fixie_chat(ctx)
        case _:
            raise ValueError(f"Unknown model: {model}")
    name = args.display_name or make_display_name(provider, model)
    return ApiContext(session, index, name, func, args, prompt or "", files or [])
