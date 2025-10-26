# SluiceGate — Zero‑Loss Peak Smoothing (In Progress...)

A pragmatic design to keep user‑visible latency and error rates stable during sudden traffic spikes **without permanent over‑provisioning**. When the cluster runs hot, a portion of requests is **buffered** (via Kafka) and **replayed** at a controlled pace after recovery. Metrics and alerts drive the switch automatically.

---

## What you get

* **Zero request loss with eventual completion** (at‑least‑once semantics via Kafka).
* **Lower 5xx during spikes** and protected p95 latency by offloading part of the traffic.
* **Cost deferral** by smoothing short‑lived peaks instead of buying peak capacity.

---

## Architecture (at a glance)

```
        ┌──────────────┐          (hot ≥95%)          ┌────────────────────┐
Users → │  NGINX  LB   │ ────────── weight 50% ─────→ │ External Buffer Svc │
        │ (outside k8s)│                             │  (HTTP → Kafka)     │
        └─────┬────────┘                              └──────────┬─────────┘
              │ weight 50% (normal path)                         │
              v                                                  (Kafka)
        ┌──────────────┐                                         │
        │  K8s Nodes   │ ◄────────── replay (rate‑limited) ◄─────┘
        │  (NodePort)  │     every 2m CronJob → NGINX LB
        └─────┬────────┘
              │
              v
           App Pods

Observability: vmagent scrapes node_exporter & cAdvisor → VictoriaMetrics. vmalert evaluates rules → Alertmanager → webhook to
`lb-controller` inside k8s → SSH to NGINX host → runs toggle script (sets weights).
```

Observability: vmagent scrapes node_exporter & cAdvisor → VictoriaMetrics. vmalert evaluates rules → Alertmanager → webhook to
`lb-controller` inside k8s → SSH to NGINX host → runs toggle script (sets weights).


1. **Users → External NGINX Load Balancer (LB).**
2. **Normal path:** LB forwards to the app running in Kubernetes (Service: ClusterIP, exposed to LB via NodePort).
3. **Hot path (triggered by alerts):** LB temporarily sends a fixed portion (e.g., 50%) to a **buffer service** outside the cluster that accepts quickly and writes to **Kafka**.
4. **Replay:** A consumer inside Kubernetes periodically (e.g., every 2 minutes) **replays** buffered requests back to the LB at a safe rate.
5. **Control loop:** Kubernetes metrics → VictoriaMetrics storage → vmalert evaluates → Alertmanager webhook → a small controller in‑cluster **toggles LB weights** on the external NGINX host.

---

## Components

* **Application**: your Java service in Kubernetes (ClusterIP Service). Exposed to the external LB using **NodePort**.
* **External NGINX LB**: routes between (a) NodePort backends (app) and (b) the external buffer service.
* **Buffer Service (external)**: accepts requests immediately (e.g., returns 202) and enqueues them into **Kafka**.
* **Kafka**: central, durable queue (replication factor ≥ 3 recommended).
* **Replay Consumer (in‑cluster)**: runs on a schedule and replays buffered requests to the LB; rate‑limited/back‑pressure aware.
* **Observability**: **vmagent** scrapes metrics, **VictoriaMetrics** stores them, **vmalert** evaluates alert rules, **Alertmanager** sends webhooks.
* **LB Controller (in‑cluster)**: receives Alertmanager webhooks and **executes a remote action** (e.g., SSH command or HTTP call) on the NGINX host to switch weights.

---

## Prerequisites

* Kubernetes cluster with ≥3 nodes.
* An **external NGINX host** reachable from the cluster (for weight toggling and as the public entry point).
* A reachable **Kafka** cluster (managed or self‑hosted) and a topic to hold buffered requests.
* Cluster RBAC permissions for the controller; secure credentials (SSH key or API token) for LB changes.

---

## Setup — High‑Level Steps

1. **Create namespaces** for `observability`, your app (e.g., `compute`), and `sluicegate` (controller).
2. **Expose the app to the LB**: convert the app Service to **NodePort** and note the assigned port and node IPs.
3. **Install VictoriaMetrics stack**: deploy **vmagent**, **VictoriaMetrics** storage, **vmalert**, and **Alertmanager**. Ensure vmagent scrapes:

   * **node‑exporter** (per‑node CPU/memory),
   * **kubelet/cAdvisor** (container CPU/memory),
   * optionally **kube‑state‑metrics** (capacity/requests), and ingress/controller metrics if present.
4. **Define alert rules** with hysteresis:

   * **High‑load alert** when average cluster CPU **or** memory utilization ≥ **95%** for a sustained window (e.g., 10 minutes).
   * **Recovered alert** when both CPU and memory drop below **80%** for the same window.
   * Route both alerts in **Alertmanager** to a dedicated **webhook receiver**.
5. **Deploy the LB controller (in cluster)**:

   * Expose an internal HTTP endpoint that Alertmanager can call.
   * The controller, upon receiving **High‑load**, sets the LB split to **N% buffer** / **(100−N)% app**; upon **Recovered**, sets it back to **0% buffer**.
   * The controller performs the LB change by calling a **local script or API on the NGINX host** (e.g., via SSH or HTTPS).
6. **Prepare the external NGINX host**:

   * Configure an **upstream** with your three NodePort backends (the K8s nodes) and one backend pointing to the **buffer service**.
   * Provide a **safe mechanism** (script or API) to update per‑backend weights and reload NGINX.
   * Start with **buffer weight = 0%** (all traffic to app).
7. **Stand up the external buffer service**:

   * Receives requests from NGINX during high‑load.
   * Immediately acknowledges to clients and writes a compact representation of the request into **Kafka** (headers/body normalized and redacted as needed).
8. **Provision Kafka**:

   * Use a topic dedicated to buffered requests.
   * Ensure durability semantics (replication, retention sized for the worst burst + drain time, client acks/retries).
9. **Deploy the replay consumer (in cluster)**:

   * Run on a schedule (e.g., **every 2 minutes**) or as a continuously running Deployment.
   * Reconstruct and send requests back to the **LB** (not directly to pods) at a controlled rate.
   * Implement **back‑off** if LB returns 5xx/429 or if SLOs degrade; observe queue lag/age.
10. **Test the loop end‑to‑end**:

    * Manually trigger the controller (simulate the webhook) to verify the LB weight change.
    * Generate synthetic load until the high‑load alert fires; confirm LB switches, buffer receives requests, Kafka accumulates lag, and the consumer drains after recovery.

---

## Operating Modes

* **Normal**: all traffic → app; buffer weight = 0%; consumer idle or draining small backlog.
* **High‑load**: LB sends a fixed share (e.g., 50%) → buffer; app receives less traffic; backlog grows in Kafka.
* **Recovery**: alert clears; LB returns to 0% buffer; consumer drains the backlog at a safe rate until empty.

---

## What to Measure (no query code)

* **Ingress request latency**: p50/p95/p99 from request duration histograms.
* **Availability**: success responses ÷ total responses over the SLO window.
* **Error rate**: 5xx/429 during spikes vs baseline.
* **Kafka health**: consumer lag and **max message age** (seconds) to ensure bounded backlog.
* **Zero‑loss assertion**: counters for **enqueued**, **replayed**, and **dropped** (should be zero) requests.

---

## Alerting Guidance

* Trigger on **utilization** (CPU/memory) **and/or symptoms** (ingress errors, throttling) with **hysteresis** to avoid flapping.
* Page on cluster‑level saturation; notify (non‑page) for per‑node hotspots.
* Route **High‑load** and **Recovered** alerts to the controller’s webhook.

---

## Security & Compliance

* **Redact sensitive data** before enqueue (auth headers, cookies, PII).
* **Encrypt in transit** (LB↔buffer, buffer↔Kafka, consumer↔LB) as needed.
* Restrict **RBAC** and **network access**: only the controller can reach the LB’s management interface; only buffer/consumer can reach Kafka.
* Limit the controller’s remote privileges to “update weights + reload” on the NGINX host.

---

## Capacity Planning (back‑of‑envelope)

* Estimate **excess RPS** during spikes and **duration** → compute worst‑case backlog.
* Size Kafka retention/disk to hold that backlog with headroom.
* Set a **drain‑time target** and tune consumer rate or concurrency to meet it without violating SLOs.

---

## Failure Modes & Safeguards

* **Kafka unavailable**: fail closed (buffer weight forced to 0%) or return 503/429 from buffer path based on policy.
* **Consumer down**: alert on lag/age; backlog stops draining until it recovers.
* **Backlog too old/large**: enforce TTL or circuit‑break (LB stops sending to buffer) to protect UX.
* **Node failure**: no request loss with Kafka replication and acks; LB still has remaining NodePort backends.

---

## Variations (choose what fits your environment)

* Use **header/path‑based routing** so only **idempotent** endpoints are buffered.
* Replace the external NGINX with a **Kubernetes Ingress controller** that supports dynamic weights (simplifies ops if Ingress is acceptable).
* Use a **managed queue** instead of self‑hosted Kafka to reduce operational overhead.
* Make the consumer **adaptive** (e.g., target CPU utilization or latency instead of a fixed schedule).

---

## Troubleshooting Checklist

* **No metrics/alerts**: confirm vmagent target discovery and that vmalert can reach VictoriaMetrics.
* **LB didn’t switch**: verify webhook delivery in Alertmanager, controller logs, and that the LB host accepted the change.
* **Requests not draining**: check consumer logs, Kafka lag/age, and LB responses (429/5xx indicate pushback).
* **High error rate persists**: lower buffer share, prioritize idempotent routes, or reduce consumer rate.

---

## Summary

SluiceGate gives you a **repeatable pattern** to turn bursty traffic into a manageable backlog while protecting user experience and controlling cost. It’s intentionally modular: the LB, buffer, queue, consumer, and control loop can be swapped for equivalents that suit your platform and governance.
