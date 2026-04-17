<p align="center">
  <a href="https://voipbin.net">
    <img alt="VoIPBin" src="https://voipbin.net/android-chrome-192x192.png" width="120" />
  </a>
</p>

<h1 align="center">VoIPBin</h1>

<h3 align="center">The Open-Source CPaaS Platform — Built for Enterprises, Open for Everyone</h3>

<p align="center">
A complete, production-grade Communications Platform as a Service: <b>Voice</b>, <b>SMS</b>, <b>Video</b>, <b>AI</b>, <b>Team Messaging</b>, and <b>Conferencing</b> — fully self-hostable, API-first, and running in production today.
</p>

<p align="center">
  <a href="https://voipbin.net">Website</a> •
  <a href="https://api.voipbin.net/docs/">API Docs</a> •
  <a href="https://admin.voipbin.net">Admin Console</a> •
  <a href="https://talk.voipbin.net">Talk</a> •
  <a href="https://meet.voipbin.net">Meet</a>
</p>

<p align="center">
  <a href="https://admin.voipbin.net"><img src="https://img.shields.io/badge/Live-admin.voipbin.net-success?logo=statuspage&logoColor=white" alt="Live Demo" /></a>
  <a href="https://github.com/voipbin/voipbin/stargazers"><img src="https://img.shields.io/github/stars/voipbin/voipbin?style=social" alt="GitHub Stars" /></a>
  <a href="https://circleci.com/gh/voipbin/monorepo"><img src="https://img.shields.io/circleci/build/github/voipbin/monorepo?label=build" alt="Build Status" /></a>
  <a href="https://github.com/voipbin/voipbin/blob/main/LICENSE"><img src="https://img.shields.io/github/license/voipbin/voipbin?color=blue" alt="License" /></a>
  <a href="https://github.com/voipbin/monorepo"><img src="https://img.shields.io/badge/microservices-34-orange" alt="Microservices" /></a>
  <a href="https://github.com/voipbin/voipbin/issues"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome" /></a>
</p>

<p align="center">
  <b>🟢 Live production instance:</b> <a href="https://admin.voipbin.net">admin.voipbin.net</a> — Try it now with the built-in <b>guest account</b> (no signup required)
</p>

<br />

<p align="center">
  <a href="https://admin.voipbin.net">
    <img src="docs/images/dashboard-tour.gif" alt="VoIPBin Admin Console Tour" width="800" />
  </a>
</p>

---

## Why VoIPBin?

**Most CPaaS platforms come with trade-offs**: vendor lock-in, unpredictable pricing, and zero control over your infrastructure. Most open-source alternatives either stop at SIP or require gluing together a dozen unrelated projects.

**VoIPBin is different.** It's the **only production-grade, self-hostable, all-in-one CPaaS** — voice, messaging, video, AI, team collaboration, and conferencing in a single coherent platform. 34 Go microservices running on Kubernetes, backed by Asterisk, Kamailio, and RTPEngine, fully open-sourced under MIT.

> _"Own your communications stack."_ — Run your own CPaaS with full API control and zero vendor lock-in.

### What makes VoIPBin enterprise-ready:

- 🏭 **Running in production today** — Serving real traffic at [voipbin.net](https://voipbin.net), not a weekend prototype
- 🔓 **Truly open-source** — MIT Licensed, no "open-core" bait-and-switch, every component is in the repo
- 🧩 **Complete platform, not a toolkit** — Voice, SMS, Video, AI, Queues, Campaigns, Team Messaging, Meetings — all integrated
- 🤖 **AI-native** — Built-in AI assistants, real-time transcription, post-call summarization, intelligent routing
- 🏢 **Multi-tenant by design** — Full customer isolation, billing, quotas, and access control out of the box
- ☸️ **Cloud-native & horizontally scalable** — Kubernetes-first, stateless services, message-queue backbone
- 📞 **Carrier-grade voice** — Asterisk + Kamailio + RTPEngine with SRTP, OPUS, PCMU/PCMA, WebRTC
- 🛡️ **Data sovereignty** — Deploy on your own infrastructure. No data leaves your cloud.

---

## ✨ Features — A Unified CPaaS, Not a Collection of Parts

Everything your communications stack needs, built to work together out of the box.

<table>
<tr>
<td width="50%">

### 📞 Voice & Telephony
- **Programmable Call Flows** — Design advanced call logic with branching, loops, and post-call hooks using declarative JSON
- **Call Queues & Agents** — Priority-based routing, ring strategies, agent login/logout
- **Call Recording** — Record, transcribe, and summarize conversations
- **Conferencing** — Secure real-time audio/video with moderation tools
- **Extension Management** — SIP/WebRTC registration and routing
- **Carrier-grade media** — SRTP, OPUS, PCMU/PCMA codecs

</td>
<td width="50%">

### 💬 Messaging & Channels
- **SMS & Messaging Flows** — Same powerful flow engine for messages
- **Chat & Web Messaging** — Real-time customer-agent communication
- **Email Integration** — Multichannel notification support
- **Webhook & HTTP** — Trigger actions and integrate with external services
- **Campaign Automation** — Bulk voice/SMS campaigns via API
- **Inbound & Outbound** — Full two-way communication support

</td>
</tr>
<tr>
<td width="50%">

### 🤖 AI & Intelligence
- **AI-Powered Voice Assistants** — Conversational AI with smart action triggers
- **Real-time Transcription** — Live speech-to-text during calls
- **Post-call Summarization** — AI-generated call summaries
- **Intelligent Flow Routing** — Context-aware decision making
- **RAG-backed Assistants** — Ground AI in your knowledge base
- **MCP Integration** — Plug VoIPBin into any AI agent via Model Context Protocol

</td>
<td width="50%">

### 💼 Talk — Team Collaboration
- **Channels & Threads** — Organized team messaging with threaded replies
- **Rich Media Sharing** — Images, videos, files, and link previews
- **Integrated Voice Calls** — Place and receive calls without leaving the app
- **Agent Workspace** — Unified inbox for chats, calls, and tasks
- **Multi-tenant Messaging** — Isolated workspaces per customer
- **Web-based — no installs** — Works in any modern browser

</td>
</tr>
<tr>
<td width="50%">

### 🎥 Meet — Video Conferencing
- **Browser-based Video Calls** — No downloads, works everywhere via WebRTC
- **HD Audio & Video** — Low-latency real-time media
- **Screen Sharing & Presentations** — Share your screen in one click
- **Moderation Tools** — Host controls, mute, remove participants
- **Shareable Links** — Join meetings via simple URL
- **Integrated with Voice** — Escalate chats into full video meetings

</td>
<td width="50%">

### 🏢 Platform & Operations
- **Multitenancy** — Isolated configs, flows, agents per tenant
- **Billing Management** — Built-in usage tracking and billing
- **Number Management** — Phone number provisioning and routing
- **Role-based Access Control** — Fine-grained permissions
- **Observability** — Metrics, logs, and tracing hooks
- **API-first** — Everything is scriptable via REST API

</td>
</tr>
</table>

---

## 🎬 See It in Action

VoIPBin ships with three production-ready applications — all backed by the same API and microservices layer.

### 🔧 Admin Console — Build and Manage Your CPaaS

The operator workspace for building call flows, managing AI assistants, configuring agents, and monitoring your entire communications stack.

<a href="https://admin.voipbin.net"><b>🔧 Try Admin Console →</b></a> &nbsp;·&nbsp; <sub>Guest account available — no sign-up required</sub>

<p align="center">
  <a href="https://admin.voipbin.net">
    <img src="docs/images/admin-slideshow.gif" alt="Admin Console Tour" width="820" />
  </a>
</p>

### 💼 Talk — The Agent & Team Workspace

The collaboration hub for agents and teams — real-time messaging with **threaded conversations**, **rich media sharing**, and **integrated voice calls**, all in one browser tab.

<a href="https://talk.voipbin.net"><b>💼 Try Talk →</b></a> &nbsp;·&nbsp; <sub>Guest account available — no sign-up required</sub>

<p align="center">
  <a href="https://talk.voipbin.net">
    <img src="docs/images/talk-slideshow.gif" alt="Talk Feature Tour" width="820" />
  </a>
</p>

### 🎥 Meet — Browser-Based Video Conferencing

WebRTC-powered video conferencing — HD audio and video, screen sharing, and shareable meeting links. No downloads, no plugins, no vendor lock-in.

<a href="https://meet.voipbin.net"><b>🎥 Try Meet →</b></a> &nbsp;·&nbsp; <sub>No sign-up required — join instantly</sub>

<p align="center">
  <a href="https://meet.voipbin.net">
    <img src="docs/images/meet-slideshow.gif" alt="Meet Feature Tour" width="820" />
  </a>
</p>

---

## 🚀 Two Ways to Use VoIPBin

VoIPBin offers two deployment options depending on your needs:

<table>
<tr>
<td align="center" width="50%">

### ☁️ VoIPBin Cloud

**The fastest way to get started.**

Use VoIPBin as a fully managed service — no infrastructure to set up, no servers to maintain. Just sign up and start building.

✅ Instant setup — start in minutes<br/>
✅ No infrastructure management<br/>
✅ Auto-scaling & high availability<br/>
✅ Always up-to-date<br/>
✅ Demo account available

<br/>

<a href="https://admin.voipbin.net"><b>🔗 Try VoIPBin Cloud →</b></a>

</td>
<td align="center" width="50%">

### 🏠 Self-Install

**Full control over your infrastructure.**

Deploy VoIPBin on your own cloud infrastructure. Own your data, customize everything, and run it wherever you want.

✅ Complete data ownership<br/>
✅ Currently supports GCP (AWS, Azure planned)<br/>
✅ Full customization & white-labeling<br/>
✅ No usage-based fees<br/>
✅ Air-gapped / private network support

<br/>

<a href="#-self-install-guide"><b>🔗 Self-Install Guide →</b></a>

</td>
</tr>
</table>

---

## ☁️ VoIPBin Cloud — Quick Start

Get started with VoIPBin Cloud in 3 steps:

**1. Sign up at [admin.voipbin.net](https://admin.voipbin.net)**

A demo account is available — no credit card required.

**2. Get your API credentials**

After signing in, grab your API token from the Admin Console dashboard.

**3. Make your first API call**

```bash
export VOIPBIN_TOKEN="your_api_token_here"
```

```bash
# List your registered numbers
curl -X GET https://api.voipbin.net/v1.0/numbers \
  -H "Authorization: Bearer $VOIPBIN_TOKEN"
```

```bash
# Create a programmable call flow
curl -X POST https://api.voipbin.net/v1.0/flows \
  -H "Authorization: Bearer $VOIPBIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Hello World",
    "actions": [
      {"type": "talk", "text": "Hello from VoIPBin!"}
    ]
  }'
```

> 📘 **Full API Reference**: [api.voipbin.net/docs](https://api.voipbin.net/docs/) — Explore all endpoints interactively.

---

## 🏠 Self-Install Guide

Deploy VoIPBin on your own cloud with a **single CLI command**. The [**voipbin/install**](https://github.com/voipbin/install) repo handles everything — infrastructure provisioning, VM configuration, and full Kubernetes deployment.

### 3 Commands to Deploy

```bash
# Clone the installer
git clone https://github.com/voipbin/install.git
cd install
pip install -r requirements.txt

# Step 1: Interactive setup wizard (7 questions)
./voipbin-install init

# Step 2: Deploy everything
./voipbin-install apply

# Step 3: Verify deployment health
./voipbin-install verify
```

The `init` wizard guides you through: GCP project, region, cluster type, TLS, domain, and DNS configuration. Then `apply` runs a fully automated 3-stage pipeline:

```
Stage 1: Terraform          Stage 2: Ansible          Stage 3: Kubernetes
─────────────────           ────────────────          ───────────────────
VPC, GKE, Cloud SQL         Kamailio VMs              34 Backend Services
Firewall, DNS, LB           RTPEngine VMs             3 Asterisk Instances
NAT, KMS, Storage           Docker + Config           3 Frontend Apps
                                                      Redis, RabbitMQ, etc.
```

### Prerequisites

| Tool | Version | Install |
|---|---|---|
| **gcloud CLI** | >= 400.0 | [cloud.google.com/sdk](https://cloud.google.com/sdk/docs/install) |
| **Terraform** | >= 1.5.0 | [hashicorp.com](https://developer.hashicorp.com/terraform/downloads) |
| **Ansible** | >= 2.15.0 | `pip install ansible` |
| **kubectl** | >= 1.28.0 | [kubernetes.io](https://kubernetes.io/docs/tasks/tools/) |
| **sops** | >= 3.7.0 | [github.com/getsops](https://github.com/getsops/sops/releases) |
| **Python 3** | >= 3.10 | [python.org](https://www.python.org/downloads/) |

### What Gets Deployed

| Layer | Components |
|---|---|
| **Backend** | 34 Go microservices (call, flow, AI, queue, campaign, billing, talk, etc.) |
| **VoIP** | Asterisk (call, conference, registrar) + Kamailio + RTPEngine |
| **Frontend** | Admin Console, Talk (agent app), Meet (video conferencing) |
| **Infrastructure** | Redis, RabbitMQ, ClickHouse, Cloud SQL (MySQL), Cloud SQL Proxy |
| **Network** | VPC, Cloud NAT, Load Balancers, Firewall Rules, TLS/SSL |

### Cost Estimates

| Deployment Type | Estimated Cost |
|---|---|
| **Zonal** (testing/small) | ~$170/month |
| **Regional** (production/HA) | ~$243/month |

> **Currently supported:** Google Cloud Platform (GCP)<br/>
> **Planned:** AWS, Azure, and more
>
> 📖 **Full documentation**: See the [**voipbin/install**](https://github.com/voipbin/install) repo for detailed architecture, configuration reference, troubleshooting, and cost breakdowns.

---

## 🏗️ Architecture

VoIPBin is built as a distributed system of **34 Go microservices**, communicating via message queues and REST APIs, all orchestrated on Kubernetes.

```
┌─────────────────────────────────────────────────────────┐
│                     Client Layer                        │
│   Admin Console  •  Talk  •  Meet  •  SDK  •  REST API  │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                    API Gateway                          │
│              (bin-api-manager)                          │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│              Core Microservices                         │
│                                                         │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│   │   Call   │ │   Flow   │ │   AI     │ │  Queue   │   │
│   │ Manager  │ │ Manager  │ │ Manager  │ │ Manager  │   │
│   └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
│                                                         │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│   │ Campaign │ │Conference│ │ Message  │ │ Customer │   │
│   │ Manager  │ │ Manager  │ │ Manager  │ │ Manager  │   │
│   └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
│                                                         │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│   │ Billing  │ │  Number  │ │   Talk   │ │  Hook    │   │
│   │ Manager  │ │ Manager  │ │ Manager  │ │ Manager  │   │
│   └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
│                                                         │
│         ... and 22 more microservices                   │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                 Media & VoIP Layer                      │
│       Asterisk  •  Kamailio  •  RTPEngine               │
└─────────────────────────────────────────────────────────┘
```

> 📖 See the full [architecture documentation](https://github.com/voipbin/monorepo) for detailed service descriptions.

---

## 📦 Repositories

| Repository | Description | Stars |
|---|---|---|
| **[voipbin/voipbin](https://github.com/voipbin/voipbin)** | 📍 You are here — project overview and documentation | ![Stars](https://img.shields.io/github/stars/voipbin/voipbin?style=flat-square) |
| **[voipbin/install](https://github.com/voipbin/install)** | Self-install guide and deployment scripts | ![Stars](https://img.shields.io/github/stars/voipbin/install?style=flat-square) |
| **[voipbin/monorepo](https://github.com/voipbin/monorepo)** | Backend microservices (34 Go services) | ![Stars](https://img.shields.io/github/stars/voipbin/monorepo?style=flat-square) |
| **[voipbin/voipbin-go](https://github.com/voipbin/voipbin-go)** | Go SDK for VoIPBin API | ![Stars](https://img.shields.io/github/stars/voipbin/voipbin-go?style=flat-square) |
| **[voipbin/mcp](https://github.com/voipbin/mcp)** | MCP (Model Context Protocol) server | ![Stars](https://img.shields.io/github/stars/voipbin/mcp?style=flat-square) |
| **[voipbin/sandbox](https://github.com/voipbin/sandbox)** | Sandbox & examples | ![Stars](https://img.shields.io/github/stars/voipbin/sandbox?style=flat-square) |

---

## 📚 Documentation

- 📘 **[API Reference](https://api.voipbin.net/docs/)** — Explore and test all VoIPBin APIs
- 🏗️ **[Architecture Guide](https://github.com/voipbin/monorepo)** — System design and service breakdown
- 🐍 **[Python Examples](https://github.com/voipbin/sandbox)** — Sample applications and integrations

---

## 🤝 Contributing

We welcome contributions of all kinds! Whether it's fixing a bug, improving documentation, or proposing new features.

All source code lives in the individual repositories — this repo is the project hub with no code of its own. To contribute, head to the repo that matches what you want to work on:

| What you want to work on | Repo |
|---|---|
| Backend microservices (Go) | **[voipbin/monorepo](https://github.com/voipbin/monorepo)** |
| Go SDK | **[voipbin/voipbin-go](https://github.com/voipbin/voipbin-go)** |
| MCP server | **[voipbin/mcp](https://github.com/voipbin/mcp)** |
| Deployment / self-hosting | **[voipbin/install](https://github.com/voipbin/install)** |
| Examples & sandbox | **[voipbin/sandbox](https://github.com/voipbin/sandbox)** |

Fork the relevant repo, create a feature branch, and open a PR there. Have a question first? Join our [Discord](https://discord.com/invite/waztvb63Yx) — we're happy to help.

---

## ⭐ Star VoIPBin

If VoIPBin saves you from reinventing a CPaaS, help others find it by starring the repo. It genuinely helps the project grow.

<p align="center">
  <a href="https://github.com/voipbin/voipbin/stargazers">
    <img src="https://img.shields.io/github/stars/voipbin/voipbin?style=social" alt="GitHub Stars" />
  </a>
</p>

---

## 📫 Contact

- 🌐 Website: [voipbin.net](https://voipbin.net)
- 📧 Email: [support@voipbin.net](mailto:support@voipbin.net)
- 🐙 GitHub: [@voipbin](https://github.com/voipbin)
- 💬 Discord: [Join our community](https://discord.com/invite/waztvb63Yx)

---

## 📄 License

VoIPBin is open-source software licensed under the [MIT License](LICENSE).

<p align="center">
  <sub>Built with ❤️ by <a href="https://github.com/pchero">@pchero</a> — CPaaS for All</sub>
</p>
