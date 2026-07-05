# Module 8 — Deployment

The agent works. It answers questions, retrieves context, falls back across
providers, retries transient errors, and logs what it did. Every prior module
made it *do more*. This module makes it *survive leaving your machine*: cross a
network boundary, live inside a container, run under an orchestrator, and answer
at a URL other people can call — including when your laptop is off.

## The shift this module makes

The mindset changes from "does it work?" to "does it work when I'm not
watching?" A script that runs under your fingers has a lot of implicit
scaffolding: your Python, your environment variables, your network, you there to
restart it when it dies. Production removes all of that. The agent has to declare
what it needs, start without hand-holding, tell an orchestrator when it's healthy
and when it's ready, shut down cleanly when asked, and cost a predictable amount.
None of that is about intelligence; all of it is about operability.

## The progression

Each stage below solves a problem the previous stage exposes. That's the spine of
the module.

```
Agent  ──►  Service  ──►  Container  ──►  Orchestrator  ──►  Cloud
(logic)     (HTTP +       (portable,      (scaling,          (managed,
             probes)       reproducible)   self-healing)      scale-to-zero)
```

- **Agent → Service.** A function isn't callable over a network. Wrap it in
  FastAPI so it has an address, and add the two probes an orchestrator needs.
- **Service → Container.** "Works on my machine" isn't a deployment. A container
  freezes the OS, Python, and dependencies into one reproducible artifact.
- **Container → Orchestrator.** One container on one host dies with that host. An
  orchestrator runs several, restarts the dead ones, and scales with load.
- **Orchestrator → Cloud.** Running your own cluster is work. A managed platform
  (Cloud Run) takes the container and handles the rest, scaling to zero when idle.

The files in this module walk that path in order: `01_agent.py` and
`02_service.py` (agent + service), `03_Dockerfile` and `05_docker-compose.yml`
(container + local orchestration), `06_k8s/` (Kubernetes), and
`07_deploy_cloudrun.md` (cloud).

## The service contract: liveness vs readiness

The agent from earlier modules is inlined into `01_agent.py` as a
**self-contained** version — no cross-module imports, no vector store, no
filesystem state — because each of those is a thing that breaks inside a
container. The telemetry, retry, and exact-match cache patterns from Module 5 are
carried in directly; RAG retrieval is replaced by a tiny inline fact set so the
agent answers grounded questions with zero external dependencies.

`02_service.py` wraps it in FastAPI and exposes two health endpoints that look
similar and mean very different things:

- **`GET /health` — liveness.** "Is the process alive?" Cheap, answers instantly
  if the event loop is running. If it fails, the orchestrator **restarts** the
  container.
- **`GET /ready` — readiness.** "Can I serve a real request *right now*?" For an
  agent that means: has a model provider been health-checked and **bound**? If it
  fails, the orchestrator keeps the container running but routes **no traffic** to
  it until it passes.

The distinction is the single most important idea in the module. A container can
be alive but not ready — the process is fine, it just hasn't bound a model yet.
Point both probes at one endpoint and the orchestrator will restart containers
that are merely still warming up, producing a restart loop that never resolves.
Readiness maps exactly onto the startup health-check protocol from
`lib/providers.py`: *ready* means *a model in the role's preference chain
answered and got bound*.

Two more service-level production habits live in `02_service.py`: **graceful
shutdown** (on the SIGTERM every orchestrator sends to stop a container, stop
accepting new requests and let in-flight ones finish), and **structured logging**
— one JSON line per request to stdout, which the container platform collects
automatically. In a container, "structured logging" is often just: print JSON to
stdout and let the platform route it.

## Containerization

`03_Dockerfile` is a **multi-stage** build. A fat *builder* stage installs
dependencies; a slim *runtime* stage copies only the installed packages and the
app code, leaving compilers and build caches behind. The result is smaller
(faster to pull, less to patch, less attack surface) and cleaner. The choices
that matter, each for a reason:

- **Pin the base image** (`python:3.11-slim`, never `:latest`) so builds are
  reproducible.
- **Install dependencies in their own layer, before copying code**, so editing
  your code doesn't invalidate the cached dependency install.
- **Run as a non-root user**, so a compromised process doesn't own the container.
- **Read `$PORT` from the environment** (Cloud Run injects it) and **bind
  `0.0.0.0`**, because a process on `127.0.0.1` is invisible from outside the
  container.

`04_dockerignore.txt` (rename to `.dockerignore`) keeps secrets and cruft out of
the build context — the most important line in it is the one excluding `.env`, so
your keys can never reach an image layer.

## Local orchestration: docker-compose and the resilience floor

`05_docker-compose.yml` runs two services on one network: the **app** and
**Ollama**. Ollama isn't there for dev convenience — it's a **resilience floor**.
The provider chain in `lib/providers.py` already falls back, in order, to a local
Ollama model when hosted providers fail. Wiring Ollama into the compose network
means the service keeps answering even if every hosted provider is down,
rate-limited, or unpaid. An expired invoice shouldn't take the service offline.
That's a real availability property.

The honest caveat, carried through the whole module: this floor holds only where
you control a host with enough memory to run the model — a laptop, a VM, a
Kubernetes node sized for it. It does **not** translate to a cheap scale-to-zero
Cloud Run container. There the floor becomes a cheap *hosted* small model instead.
Same principle, different implementation, because the environment is different.

## Kubernetes

`06_k8s/` is a complete, applyable bundle — the reusable, production-shaped
version you'd lift into a real product:

- **`00_namespace`** isolates the app's objects.
- **`01_configmap`** holds non-secret config; **`02_secret.example`** is a
  template for keys (copy it, fill it, keep the copy out of git).
- **`03_deployment`** runs two replicas and wires the probes: a **startupProbe**
  grants a warm-up window, **livenessProbe → `/health`** restarts wedged pods,
  **readinessProbe → `/ready`** holds traffic until a model binds. It sets CPU/
  memory **requests** (which drive scheduling and autoscaling) and **limits**
  (which cap usage), and runs non-root.
- **`04_service`** gives the pods one stable in-cluster address.
- **`05_hpa`** adds and removes pods based on CPU against the requests — which is
  why the requests have to be set.
- **`06_ingress`** exposes the service publicly. It uses **nginx-ingress**,
  chosen because it's the most portable controller (runs the same on a laptop,
  on GKE/EKS/AKS, and on-prem); the file notes exactly how to swap in a
  cloud-managed controller instead.

You'd reach for Kubernetes when you want control, portability, and the Ollama
floor — at the cost of running the cluster yourself.

## Cloud Run

`07_deploy_cloudrun.md` is the serverless alternative: hand the container to a
managed platform and let it scale, including **to zero** when idle. For bursty
agent traffic that's usually the better trade, and it's essentially free to test
— the free tier covers a demo, scale-to-zero means you pay only while processing
a request, and two flags (`--max-instances` low, `--min-instances 0`) keep the
cost ceiling predictable and the idle cost at zero. The guide has the real deploy
commands, Secret Manager wiring, and a cost-safety section worth reading before
you run it.

## Findings

The honest war stories are collected in `LEARNING_LOG.md`. In brief: pointing
liveness and readiness at one endpoint causes restart loops; the startup
health-check that's ideal on a long-running pod fights scale-to-zero on Cloud
Run; a missing `.dockerignore` or a committed `secret.yaml` leaks keys into
places that are effectively permanent; `min-instances > 0` is a silent 24/7 bill;
uncapped autoscaling scales for attackers too; the resilience floor has to change
with the environment; and binding `127.0.0.1` instead of `0.0.0.0` makes a
container that times out with nothing in the logs.

## Run it yourself

The ladder, each rung adding one layer of the progression:

```bash
# 0) the agent alone (self-test: retrieval, cache, retry, honest failure)
python 01_agent.py

# 1) the service (then curl it)
python 02_service.py
curl localhost:8080/health          # {"status":"alive"}
curl localhost:8080/ready           # 503 until a provider is configured

# 2) the container
cp 04_dockerignore.txt .dockerignore
docker build -f 03_Dockerfile -t agentic-deploy:local .
docker run -p 8080:8080 --env-file .env agentic-deploy:local

# 3) local orchestration with the Ollama floor
docker compose -f 05_docker-compose.yml up --build
docker compose -f 05_docker-compose.yml exec ollama ollama pull qwen2.5:1.5b

# 4) Kubernetes (needs a cluster + the nginx-ingress controller)
kubectl apply -f 06_k8s/00_namespace.yaml
kubectl apply -f 06_k8s/01_configmap.yaml
kubectl create secret generic agentic-secrets -n agentic \
  --from-literal=GEMINI_API_KEY=your-key
kubectl apply -f 06_k8s/03_deployment.yaml \
              -f 06_k8s/04_service.yaml \
              -f 06_k8s/05_hpa.yaml \
              -f 06_k8s/06_ingress.yaml

# 5) Cloud Run — see 07_deploy_cloudrun.md
```

## What we deliberately didn't do

- **Service mesh (Istio/Linkerd)** — real for multi-service traffic management,
  overkill for one service.
- **Multi-region / global load balancing** — a scale and latency concern beyond a
  portfolio deployment.
- **GPU serving** — this agent calls hosted models or a small local one; no GPU
  needed.
- **Full Infrastructure-as-Code (Terraform/Pulumi)** — the right way to manage
  cloud resources at scale; the imperative `gcloud`/`kubectl` commands here are
  clearer for teaching the concepts.
- **A CI/CD pipeline** — referenced (keyless deploys, image scanning, rollout
  gates) but not built; it's a repo-wide concern, not a module artifact.

## What's next

That closes the arc. Across eight modules the repo goes from a hand-written ReAct
loop to a deployed, observable, guarded, self-healing agent that scales with load
and costs what you expect. The patterns here — the liveness/readiness split, the
multi-stage build, the environment-dependent resilience floor, the cost rails —
are the ones that separate an agent that demos from an agent that ships.

🔗 A production agent built on these patterns is in development at
[github.com/ankitpani8/chakiri-web](https://github.com/ankitpani8/chakiri-web) —
live demo coming soon.
