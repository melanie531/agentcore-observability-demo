# AgentCore Observability and HoneyHive

Checked against live official sources on **2026-09-20 AEST / 2026-09-19 UTC**. These are documented capabilities, not a claim that this demo has enabled all of them. HoneyHive UI and deployment were not tested.

**Update 2026-09-20:** the AgentCore side is now backed by a live demo deployment (`obsdemo_travel_agent`, us-west-2): instrumented model/tool spans via Transaction Search, Runtime application logs, Memory STM events + async LTM user-preference records, and explicitly configured Memory vended log delivery were all verified with fresh synthetic traffic (evidence: `build/local/evidence-manifest.json`). The "not automatically configured" caveat for Memory log destinations was confirmed in practice — we created the delivery source/destination ourselves. HoneyHive claims remain documentation-based only.

**Customer framing:** AgentCore provides AWS-integrated agent operations and telemetry; HoneyHive provides an application-focused tracing and quality-improvement workflow. Both support tracing and evaluation. Choose by operating environment, investigation workflow, evaluation collaboration and data requirements, then validate integration against the actual application.

## First distinguish six different things

| Data | What it answers | Enablement / important boundary |
|---|---|---|
| AWS service metrics | Did the Runtime/Memory/Gateway service receive requests, fail, throttle, or consume resources? | The service-provided table says metrics are provided by default. Inspect actual namespace, dimensions, operation, statistic and period. A token count from instrumented model spans is not automatically a service metric. [A1] |
| Application logs | What did application code write to stdout/stderr or structured logging? | Depends on app logging and delivery. Runtime creates a service log group by default per configuration guide; group existence does not prove application content arrived. A JSON `trace_id` alone is not proof of exported tracing. [A2] |
| OTel spans / traces | Which model, tool or other operation ran, in what parentage, and where was time spent? | Instrument agent code with supported SDK/framework integration; configure exporter, IAM and session context. Transaction Search setup is required for the documented AgentCore trace experience. Service spans do not expose every internal app step automatically. [A1, A3] |
| Memory STM events | What conversation/event data was deliberately stored for a specific actor+session? | Call CreateEvent, then scoped ListEvents/GetEvent. These are application data in Memory, **not CloudWatch logs**. Conversational events can feed LTM extraction. [A4] |
| Memory LTM records and workflow logs | What facts/preferences were extracted and consolidated, and did those processes succeed? | Existing strategies plus asynchronous extraction produce records. Memory log destinations require explicit configuration. Extraction/consolidation logs are operational evidence, separate from the records themselves. Client CreateEvent/Retrieve spans need app instrumentation; async workers need not be children in one continuous trace. [A2, A5] |
| CloudTrail audit | Who/API principal called a supported API, when, on which resource, and with what result? | Management and data events differ. Gateway data events require explicit enablement, are billable, and are absent from Event history. CloudTrail is not a model waterfall or a copy of the full conversation. Memory API coverage/selectors must be checked for the selected resource; this task did not verify them. [A6, A7] |

**Memory enablement wording:** the Memory metrics page broadly mentions instrumentation; the resource configuration page explicitly says Memory/Gateway destinations are not automatic. Treat log delivery setup and application client tracing as separate checks. Do not imply that adding an SDK alone configures Memory service log delivery.

## Source-linked fact matrix

| Customer question | AgentCore / AWS documented capability | HoneyHive documented capability | Decision / limit |
|---|---|---|---|
| Tracing and debugging | AgentCore/CloudWatch uses metrics, logs, traces and session association; instrumented model/tool spans expose app behavior. AgentCore services provide separate telemetry. [A1–A3] | Sessions group hierarchical events; wide-event records include inputs, outputs, timings, metrics and errors; OTel-based tracing. [H1] | Compare investigation on the same synthetic workflow. Parent/child layers are not necessarily duplicate model calls. |
| Model and tool performance | Supported instrumentation captures model/tool steps. Inspect actual exported fields; custom tools and cross-process context may require explicit code. [A3] | Custom spans and instrumentation extend beyond model calls to application steps. [H1, H2] | Neither product can reconstruct a missing span or uncaptured input retroactively. Check SDK/framework versions. |
| Evaluation availability | **AgentCore Evaluations GA announced March 2026**, including online/on-demand, built-in, custom LLM and Lambda code-based evaluators. [A8] | Offline/online evaluators, experiments and dataset-based evaluation workflows are documented. [H3, H4] | It is incorrect to describe AgentCore as “metrics only / no evals,” or HoneyHive as “only traces.” |
| Testing and regression | On-demand evaluation can score provided spans; online evaluation can consume configured CloudWatch sources. A current AWS blog also describes asynchronous batch evaluation and CI integration. [A9] | Run functions on datasets, associate evaluation traces, compare runs side by side, and detect quality differences. [H3, H5] | A runnable test harness and expected behavior remain necessary. Observability alone does not create useful regression cases. Batch is source-documented, not tested in this environment. |
| Datasets and human review | Reference/ground-truth inputs are documented for evaluations. [A8, A9] | Dataset workflows and annotation queues organize human labels and quality review. [H3, H6] | Reviewed AWS sources do not establish an equivalent annotation-queue UX. Do not extrapolate that to “AWS has no human evaluation.” |
| Continuous quality monitoring | Configured online evaluation samples sessions and produces results/metrics in CloudWatch. It is an explicit configuration with a role and data source. [A10] | Server-side/online evaluation is documented alongside client-side evaluation. [H4] | Verify sampling, available content, evaluator criteria, result delay and ongoing cost. This demo has no verified enabled configuration. |
| AWS operational integration | Resource telemetry for Runtime, Memory, Gateway; CloudWatch destinations/queries and AWS operational controls. [A1, A2, A5, A11] | App tracing/evaluation can include Bedrock and other providers. [H1, H2] | HoneyHive's reviewed docs do not establish native AgentCore control-plane/Memory workflow metrics as equivalent surfaces. Instrumentation may expose app calls, which is a different layer. |
| Runtime independence | AgentCore Observability documents external/on-prem/multicloud agents with supported instrumentation. [A3, A12] | App/agent observability is not limited to one model provider. [H1, H2] | “Must host on AgentCore” is too broad; some resource-specific AWS signals naturally require the corresponding AWS service. |
| Hosting and data residence | AgentCore telemetry is ingested/stored in the user's CloudWatch account; choose/inspect region, IAM, destinations and retention. [A2, A13] | Managed SaaS docs specify AWS `us-west-2`; dedicated data plane can use a chosen AWS region while control plane stays centrally managed; self-hosted deployment is documented. [H7–H10] | Do not say “HoneyHive is SaaS-only” or “all its data always stays in the customer's VPC.” Review control/data-plane boundaries, external model routes and selected deployment contract. |
| Security and content | AWS account controls do not eliminate sensitive prompt/tool output capture. Review collection, encryption and access separately from delivery. [A2, A13] | Managed docs describe encryption at rest with KMS, TLS 1.2+ and application-layer tenant isolation. Self-hosted docs describe data flow/residency. [H7, H10] | No independent security/compliance audit or contractual certification was performed. Content capture is not permission to expose customer data. |
| OTel interoperability | Supports documented SDK/framework telemetry paths. AWS explicitly says the ADOT Collector is not supported for its agent-observability setup. [A3] | HoneyHive documents an OTel-based event model and instrumentation. [H1, H2] | **Dual export is not verified.** Shared standards do not establish exporter/auth/semantic-convention/session compatibility. Do not promise a Collector fan-out recipe. |
| Retention and price | Observability charges derive from CloudWatch ingestion/storage/query/masking. Runtime, models, Memory, evaluation and audit can add costs. [A13] | Plan/contract and deployment choices require current vendor confirmation; no numeric quote was verified in this task | No universal retention, “free,” or price-comparison claim. Read actual log retention and Memory event expiration; STM expiry is not automatically LTM or log retention. |
| Console surfaces | General service table and Gateway detail page differ on Gateway GenAI-dashboard availability. [A1, A11] | Docs describe corresponding HoneyHive UI workflows. [H3, H5, H6] | AWS Gateway click route is **UI UNVERIFIED**; use CloudWatch Metrics/Logs fallback. HoneyHive UI also unverified. |

## Suggested answer to “which should we choose?”

中文：如果客户主要在 AWS 运行 AgentCore，希望从 Runtime、Memory、Gateway 的服务健康一直查到应用调用，先展示 AgentCore 与 CloudWatch 的关联。如果主要痛点是把坏回答变成数据集、让专家标注、比较 prompt/model/agent 版本，HoneyHive 的实验和人工反馈工作流值得实际试用。两边都有 evaluation；最终选择要看同一组场景里谁更快回答团队的问题，以及数据边界、接入成本和运营方式。

English: “I would start with your team's investigation and improvement workflow. AgentCore connects agent behavior to AWS service operations. HoneyHive emphasizes the loop from traces to datasets, experiments and human review. Both offer evaluation. We should test the same representative cases and review the data boundaries before choosing one, or proposing an integration.”

## Sources and dates

All links below were discovered/read through official source tools this session, unless marked as a navigation follow-up. Publication dates were not supplied for most living documentation pages. Do not treat a retrieval date as a release date.

- **A1** [Service-provided observability data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-service-provided.html) — defaults and enablement table.
- **A2** [Configure observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html) — Runtime default group; Memory/Gateway explicit destinations.
- **A3** [Get started with AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html) — SDK auto-instrumentation, session baggage, Collector limitation.
- **A4** [Create an event](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/short-term-create-event.html) — STM actor/session and immutable events.
- **A5** [Memory observability data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-memory-metrics.html) — extraction/consolidation logs and fields.
- **A6** [Gateway CloudTrail event types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-event-types.html) — explicit data events, Event history distinction.
- **A7** [Memory lifecycle policies](https://aws.amazon.com/blogs/machine-learning/designing-lifecycle-policies-for-agentcore-memory/) — example of CloudTrail-based GetMemoryRecord access tracking; sample architecture, not proof of default logging.
- **A8** [Evaluations GA announcement, March 2026](https://aws.amazon.com/about-aws/whats-new/2026/03/agentcore-evaluations-generally-available/) — supersedes preview descriptions in older notes.
- **A9** [Automated agent evaluation with AgentCore and GitHub Actions](https://aws.amazon.com/blogs/machine-learning/automated-agent-evaluation-with-amazon-bedrock-agentcore-and-github-actions/) — evaluation modes, reference data and CI workflow.
- **A10** [Custom code-based evaluators](https://aws.amazon.com/blogs/machine-learning/build-custom-code-based-evaluators-in-amazon-bedrock-agentcore/) — online configuration, source/role and CloudWatch results.
- **A11** [Gateway observability data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-gateway-metrics.html) — Gateway service metrics/logs/spans; UI wording limitation noted above.
- **A12** [On-premises/multicloud AgentCore Observability](https://aws.amazon.com/blogs/machine-learning/monitor-on-premises-and-multi-cloud-ai-agents-with-agentcore-observability/) — external agent instrumentation.
- **A13** [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) — Observability billed through CloudWatch usage; no numerical price claim in this package.
- **H1** [HoneyHive tracing concepts](https://docs.honeyhive.ai/v2/tracing/concepts.md).
- **H2** [HoneyHive official documentation index](https://docs.honeyhive.ai/llms.txt) — custom spans, MCP, multi-provider and distributed tracing guides; framework-specific integration not tested.
- **H3** [HoneyHive evaluation concepts](https://docs.honeyhive.ai/v2/evaluation/concepts.md).
- **H4** [HoneyHive evaluator types](https://docs.honeyhive.ai/v2/evaluators/introduction.md).
- **H5** [HoneyHive comparing experiments](https://docs.honeyhive.ai/v2/evaluation/comparing_evals.md).
- **H6** [HoneyHive annotation queues](https://docs.honeyhive.ai/v2/evaluation/annotation-queues.md).
- **H7** [HoneyHive managed SaaS](https://docs.honeyhive.ai/v2/setup/managed.md).
- **H8** [HoneyHive dedicated cloud](https://docs.honeyhive.ai/v2/setup/dedicated.md).
- **H9** [HoneyHive self-hosting](https://docs.honeyhive.ai/v2/setup/self-hosted.md).
- **H10** [HoneyHive data flow/residency](https://docs.honeyhive.ai/v2/setup/self-hosted/data-flow.md).
