"""Model backends for the zoning step.

Two roles, each a small class with one method, so providers can be swapped:
- an extractor reads a document and returns requested fields as JSON:
      extract(document_path, prompt, schema, validate) -> dict
- a decider picks one option and says how confident it is:
      choose(state, instructions, options) -> (option_key, confidence)

Both extractors run Gemini; they differ in how they connect and send the document:
- "gemini": the Gemini API with an API key (GEMINI_API_KEY); the document is uploaded once per run
- "gcloud": Gemini on Google Cloud Vertex AI with your gcloud login, billed to GOOGLE_CLOUD_PROJECT;
  Vertex has no file-upload API, so the document is sent inline with every request

To add a provider, write a class with the same method and add it to EXTRACTORS / DECIDERS at the bottom.
"""

import json
import math
import mimetypes
import os
import time
import urllib.error
import urllib.request
import warnings


class ModelError(Exception):
    pass


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise ModelError(f"{name} is not set (add it to web_scraper/.env)")
    return value


class GeminiExtractor:
    """Reads a whole document (PDFs included, tables and all) with Gemini, via the Gemini API key.

    Output is schema-constrained, so it's always valid JSON of the requested shape. On top of that,
    `validate` checks the values, and a failed check or bad response is retried. Rate-limit errors
    (HTTP 429, common on the free tier) are waited out separately so they don't use up those retries.

    Subclasses change how the client connects (_make_client), how a document is prepared once per
    run (_prepare) and how one request is made (_generate); the retry logic in extract() is shared.
    """

    RETRYABLE = {500, 502, 503, 504}

    def __init__(self, model="gemini-3.5-flash-lite", effort="high", max_attempts=3, max_rate_limit_waits=6):
        from google import genai  # imported here so other backends don't need this package
        from google.genai import errors, types

        self.genai, self.errors, self.types = genai, errors, types
        self.client = self._make_client()
        self.model, self.effort = model, effort
        self.max_attempts, self.max_rate_limit_waits = max_attempts, max_rate_limit_waits
        self._documents = {}  # document path -> prepared document; each is prepared once per run

    def _make_client(self):
        return self.genai.Client(api_key=_require_env("GEMINI_API_KEY"))

    def _prepare(self, path):
        print(f"  uploading {path.name} to Gemini...")
        return self.client.files.upload(file=str(path))

    def _generate(self, doc, prompt, schema):
        interaction = self.client.interactions.create(
            model=self.model,
            input=[{"type": "document", "uri": doc.uri, "mime_type": doc.mime_type},
                   {"type": "text", "text": prompt}],
            response_format={"type": "text", "mime_type": "application/json", "schema": schema},
            # flash-lite defaults to "minimal" thinking, too little for big zoning tables
            generation_config={"thinking_level": self.effort},
        )
        return interaction.output_text

    def extract(self, document, prompt, schema, validate):
        attempts = rate_limit_waits = 0
        last_error = None
        while attempts < self.max_attempts:
            try:
                if document not in self._documents:
                    self._documents[document] = self._prepare(document)
                result = json.loads(self._generate(self._documents[document], prompt, schema))
                validate(result)
                return result
            except self.errors.APIError as e:
                if e.code == 429 and rate_limit_waits < self.max_rate_limit_waits:
                    rate_limit_waits += 1
                    print(f"  rate limited; waiting 60 s ({rate_limit_waits}/{self.max_rate_limit_waits})")
                    time.sleep(60)
                    continue
                if e.code not in self.RETRYABLE:
                    raise ModelError(f"Gemini API error {e.code}: {e.message}") from e
                last_error = f"Gemini API error {e.code}: {e.message}"
            except (TypeError, ValueError) as e:  # empty or non-JSON output, or failed validation
                last_error = f"invalid output: {e}"
            attempts += 1
            print(f"  attempt {attempts}/{self.max_attempts} failed ({last_error})")
            time.sleep(5 * attempts)
        raise ModelError(f"{last_error} after {self.max_attempts} attempts")


class GcloudGeminiExtractor(GeminiExtractor):
    """Gemini on Google Cloud Vertex AI, using your gcloud login (Application Default Credentials:
    run `gcloud auth application-default login` once) and billed to GOOGLE_CLOUD_PROJECT.

    Vertex has no file-upload API and this account can't use Cloud Storage, so the document is sent
    inline: read from disk once, then included in every request.
    """

    # Inline requests are capped at about 20 MB, counted after base64 encoding (which adds a third)
    MAX_REQUEST_BYTES = 20 * 1024 * 1024

    def _make_client(self):
        # This account can't set a quota project (no serviceusage.services.use permission); requests
        # still bill to GOOGLE_CLOUD_PROJECT, so google-auth's warning about it is just noise
        warnings.filterwarnings("ignore", message="Your application has authenticated using end user credentials")
        return self.genai.Client(vertexai=True, project=_require_env("GOOGLE_CLOUD_PROJECT"),
                                 location=_require_env("GOOGLE_CLOUD_LOCATION"))

    def _prepare(self, path):
        data = path.read_bytes()
        encoded_size = 4 * math.ceil(len(data) / 3)
        if encoded_size > 0.95 * self.MAX_REQUEST_BYTES:  # leave room for the prompt and schema
            raise ModelError(f"{path.name} is {len(data) / 2**20:.1f} MB, too large to send inline to Vertex "
                             f"(about 14 MB at most). Larger documents need Cloud Storage access.")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/pdf"
        return self.types.Part.from_bytes(data=data, mime_type=mime_type)

    def _generate(self, doc, prompt, schema):
        response = self.client.models.generate_content(
            model=self.model,
            contents=[doc, prompt],
            config=self.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=schema,
                # flash-lite defaults to "minimal" thinking, too little for big zoning tables
                thinking_config=self.types.ThinkingConfig(thinking_level=self.effort.upper()),
                # We use no tools; the SDK enables tool auto-calling by default and logs a notice about it
                automatic_function_calling=self.types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        return response.text


class ClefDecider:
    """Cloudflare's Clef decision model on Workers AI: picks one option and returns calibrated
    probabilities instead of generating text."""

    URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"

    def __init__(self, model="clef"):
        self.model = model
        self.url = self.URL.format(account=_require_env("CLOUDFLARE_ACCOUNT_ID"), model=model)
        self.token = _require_env("CLOUDFLARE_API_KEY")

    def choose(self, state, instructions, options):
        """`options` maps option key -> description. Returns (chosen key, confidence 0-1)."""
        body = {
            "model": self.model,
            "state": state,
            "questions": {"answer": {"type": "choice", "instructions": instructions, "criteria": options}},
        }
        request = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise ModelError(f"Clef API error {e.code}: {e.read()[:300]!r}") from e
        data = data.get("result", data)  # Cloudflare's REST API wraps payloads in {"result": ...}
        answer = data["answers"]["answer"]
        return answer["choice"], answer.get("confidence", max(answer["probabilities"].values()))


EXTRACTORS = {"gemini": GeminiExtractor, "gcloud": GcloudGeminiExtractor}
DECIDERS = {"clef": ClefDecider}
