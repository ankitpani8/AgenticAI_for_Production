# Learning Log — Deployment

The honest failure modes hit while taking a working agent from `python agent.py`
to a deployed URL. These are the things that don't show up until the agent
leaves your machine.

## Liveness and readiness are not the same probe

The first instinct is to point both probes at one `/health` endpoint. It breaks
in a specific way: while the agent is still binding a model at startup, that one
endpoint returns unhealthy, the **liveness** probe trips, and the orchestrator
**restarts the pod** — which starts the model-binding all over again. A restart
loop, caused by treating "still warming up" as "broken." The fix is the split
used here: liveness answers "is the process alive?" (cheap, always yes if the
loop runs) and readiness answers "can I serve now?" (model bound). A pod that's
alive-but-not-ready should be *waited on*, not killed. A `startupProbe` on top
gives slow starts a grace window before liveness even begins.

## The startup health-check fights scale-to-zero

The provider-selection protocol pings every provider at startup to bind a model.
On a long-running Kubernetes pod that's ideal — pay the check once. On Cloud Run
with scale-to-zero, **every cold start re-runs it**, adding latency to the first
request after every idle period, and a slow provider ping can wedge startup. The
lesson: the same startup path isn't optimal in both environments. Long-running →
health-check everything up front. Scale-to-zero → trust the first provider and
let per-call retry handle failover, or gate binding behind readiness with a
tight timeout.

## Secrets leak through the image if you let them

Two ways keys end up somewhere they shouldn't: a missing `.dockerignore` bakes
your `.env` into an image layer (and image layers are forever, even if a later
layer deletes the file), and a filled-in `secret.yaml` committed to git. Both
are prevented by discipline, not tooling: `.dockerignore` excludes `.env` and
`secret*.yaml` from the build context entirely, and the K8s secret is shipped
only as a `.example.yaml` template with `REPLACE_ME` values. Also worth knowing:
a Kubernetes Secret is base64-encoded, not encrypted — "Secret" overstates it
unless you enable encryption-at-rest.

## `min-instances > 0` is a silent bill

Cold starts are annoying, so the tempting fix is to keep one instance always
warm. On Cloud Run that means you pay for that instance 24/7 even at zero
traffic — the exact idle cost scale-to-zero exists to eliminate. For a demo it
turns a $0 month into a standing charge. Keep `min-instances 0` and accept the
cold start unless latency is a real product requirement.

## Autoscaling scales for attackers too

A public URL with uncapped `max-instances` will happily scale up to serve a bot
flood, billing you for every instance it spins up to handle the abuse. Capping
`max-instances` low trades peak throughput for a predictable cost ceiling — the
right trade for anything that isn't load-bearing production. A billing budget
alert is the backstop.

## The resilience floor is environment-dependent

Running local Ollama as the bottom of the provider chain is a real availability
win where you control the host (laptop, VM, a K8s node sized for it): if every
hosted provider is down or unpaid, the service degrades to a small local model
instead of returning errors. But it does **not** port to a cheap scale-to-zero
container, which lacks the memory to host a model and would pay the load cost on
every cold start. The principle (a cheaper always-available fallback) survives;
the implementation (local model vs. cheap hosted model) has to change with the
environment. Assuming one floor works everywhere is the mistake.

## Bind `0.0.0.0`, not `127.0.0.1`

The dev service bound `127.0.0.1` — fine on a laptop, invisible inside a
container. A process listening on loopback only accepts connections from inside
its own network namespace, so the published port maps to nothing and every
request times out with no error in the logs. Containers must bind `0.0.0.0`.
