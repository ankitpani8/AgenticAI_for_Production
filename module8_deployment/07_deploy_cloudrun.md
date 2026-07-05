# Deploying to Cloud Run

Kubernetes (the `06_k8s/` bundle) gives you control and portability. Cloud Run
gives you the opposite trade: almost no control over the infrastructure, in
exchange for almost no operational work. For a spiky, low-traffic agent, that
trade is usually worth it — and it's essentially free to test.

## Why Cloud Run suits an agent

Agent traffic is bursty: quiet for long stretches, then a flurry. Cloud Run
**scales to zero** — when no requests are arriving, no instances run and you pay
nothing. The first request after idle pays a **cold-start** latency (the
container boots, then binds a model); subsequent requests are warm. You are
billed only while a request is being processed, rounded to the nearest 100ms.

## Prerequisites

- A Google Cloud project with billing enabled (new accounts get $300 in free
  credit for 90 days, plus an always-free tier that does not expire).
- `gcloud` CLI installed and authenticated (`gcloud auth login`).
- The service listens on `$PORT` and binds `0.0.0.0` — both already handled in
  `02_service.py`.

## Deploy

The simplest path builds from source (Cloud Build makes the image for you) and
deploys in one command. Run from the module directory:

```bash
# Set your project once.
gcloud config set project YOUR_PROJECT_ID

# Deploy. Cloud Run builds the container from 03_Dockerfile, pushes it, and
# rolls it out. --source . uses the current directory as build context.
gcloud run deploy agentic-deploy \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --max-instances 2 \
  --min-instances 0 \
  --memory 512Mi \
  --cpu 1 \
  --set-env-vars "LOG_LEVEL=INFO" \
  --set-secrets "GEMINI_API_KEY=gemini-key:latest"
```

Cloud Run prints a public HTTPS URL. Test it:

```bash
URL=$(gcloud run services describe agentic-deploy --region us-central1 --format 'value(status.url)')
curl "$URL/health"                       # {"status":"alive"}
curl "$URL/ready"                         # {"ready":true,...} once a model binds
curl -X POST "$URL/invoke" -H 'content-type: application/json' \
  -d '{"query":"what is scale to zero?"}'
```

Note the readiness endpoint (`/ready`) is what a Cloud Run health check would
poll; the same `/health` vs `/ready` split from Kubernetes applies.

## Secrets

Never bake keys into the image or pass them as plain `--set-env-vars`. Store
them in Secret Manager and reference them:

```bash
echo -n "your-gemini-key" | gcloud secrets create gemini-key --data-file=-
# then the --set-secrets flag above mounts it as an env var at runtime
```

## Cost safety — read this before you deploy

Cloud Run is genuinely free for a demo, but two settings prevent surprises:

- **`--max-instances 2`** caps how far it scales. Without a cap, a traffic
  spike (or a bot hammering your public URL) scales up and bills you for every
  instance. A low cap trades throughput for a predictable ceiling — exactly what
  you want for a portfolio demo.
- **`--min-instances 0`** keeps scale-to-zero on. If you set min-instances above
  zero to avoid cold starts, those warm instances bill you around the clock even
  with zero traffic. For a demo, accept the cold start and keep this at 0.

With `min-instances 0` and a low `max-instances`, a demo that's deployed but
rarely called stays comfortably inside the free tier — realistically $0/month.
Set a billing budget alert anyway; it's one click and it's the cheapest
insurance there is.

## The Ollama-floor caveat

The `docker-compose` setup runs Ollama as a local **resilience floor**: if every
hosted provider fails, the agent falls back to a local model and keeps
answering. **That floor does not translate to Cloud Run.** A cheap scale-to-zero
container doesn't have the memory to host a model, and loading one on every cold
start fights the whole point of scale-to-zero.

On Cloud Run, the resilience floor is instead a **cheap hosted small model**
(e.g. Gemini Flash-Lite) sitting at the bottom of the provider chain, rather
than local Ollama. Same principle — a cheaper always-available fallback so an
outage or unpaid invoice on your primary provider doesn't take the service down
— realized differently because the environment is different. The floor is
environment-dependent: local model where you control the host, cheap hosted
model where you don't.
