/**
 * 51Folds Extension — Prediction engine integration (lean v1).
 *
 * Four tools (registered as prism_folds_<name> by the loader):
 *   - refine_thesis  : guides the user (via Claude) to a 51Folds-ready hypothesis
 *                      (question, outcomes, ~300-word grounding paragraph) using
 *                      the brain as context. Returns a payload ready for prism_folds_create.
 *   - create         : submits a refined thesis to the 51Folds API, registers
 *                      the resulting model in prism-brain.db's predictions table.
 *   - status         : polls platform progress for a registered model.
 *   - ingest_results : fetches completed results, ingests the narrative as a new
 *                      brain source, and wires "quantifies" edges from the
 *                      prediction node to related concepts.
 *
 * Deferred from v1 (tracked as GitHub issues): dud detection, monitor view.
 */

import {
  checkCredits,
  createModel,
  getModelProgress,
  getModelResults,
  getReport,
  createReport,
} from "./client.js";

const NUMBERED = (n) => `P${String(n).padStart(2, "0")}`;

async function nextPredictionGraphId(api) {
  const stats = await api.stats();
  const predCount = stats.node_types?.prediction || 0;
  return NUMBERED(predCount + 1);
}

async function findOrSeedPredictionDomain(api) {
  // Predictions land under a "Predictions" domain. Reuse the existing one or seed.
  const data = await api.getGraphData();
  const existing = (data.nodes || []).find(
    (n) => n.type === "domain" && /prediction/i.test(n.label || "")
  );
  if (existing) return existing.group ?? existing.group_id ?? null;
  return null;
}

export default function register(brainAPI, z) {
  return [
    // ------------------------------------------------------------------------
    // prism_folds_refine_thesis (declared as bare 'refine_thesis')
    // ------------------------------------------------------------------------
    {
      name: "refine_thesis",
      description:
        "Pull brain context relevant to a thesis the user wants to test. Returns a structured payload (question, candidate outcomes, draft 300-word grounding context) that you (Claude) refine with the user before calling prism_folds_create. The brain context surfaces what the user has already read on the topic so the grounding paragraph reflects their accumulated reading, not generic background.",
      inputSchema: z.object({
        thesis: z.string().describe("The user's thesis or hypothesis statement"),
        candidate_outcomes: z
          .array(z.string())
          .min(2)
          .max(5)
          .optional()
          .describe("2-5 candidate outcomes (e.g. ['yes','no'], ['<10%','10-30%','30-60%','>60%']). If omitted you should propose them."),
      }),
      handler: async ({ thesis, candidate_outcomes }, api) => {
        const searchResults = await api.search(thesis, 5);

        const grounding_excerpts = (searchResults || []).map((r) => ({
          source_id: r.source_id,
          source_title: r.source_title,
          excerpt: r.excerpt,
          score: r.score,
        }));

        return {
          thesis,
          candidate_outcomes: candidate_outcomes || null,
          grounding_excerpts,
          guidance: [
            "Refine `thesis` into a single falsifiable question. Time-box it. Name a mechanism.",
            "Confirm 2-5 outcomes that partition the answer space.",
            "Draft ~300 words of grounding context that synthesises grounding_excerpts. Cite source IDs inline. The 51Folds API uses this to build the causal model — vague context makes a vague model.",
            "When the user is happy with the question, outcomes, and grounding paragraph, call prism_folds_create with model_type='Advanced'.",
          ],
        };
      },
    },

    // ------------------------------------------------------------------------
    // prism_folds_create (declared as bare 'create')
    // ------------------------------------------------------------------------
    {
      name: "create",
      description:
        "Submit a refined thesis to the 51Folds API, register the resulting model in prism-brain.db's predictions table, and add a prediction node to the graph wired to brain concepts that informed the grounding paragraph. Use this only after prism_folds_refine_thesis and a Q&A round with the user produces a clean question, outcomes, and ~300-word grounding context.",
      inputSchema: z.object({
        question: z.string().describe("The single, falsifiable, time-boxed prediction question"),
        outcomes: z
          .array(z.string())
          .min(2)
          .max(5)
          .describe("2-5 outcomes that partition the answer space"),
        grounding_context: z
          .string()
          .describe("~300 words of grounding context for the API. Should cite source IDs from the brain."),
        model_type: z
          .enum(["Overview", "Insight", "Advanced"])
          .default("Advanced")
          .describe("Model complexity (Overview: fastest; Advanced: richest)"),
        cited_source_ids: z
          .array(z.string())
          .optional()
          .default([])
          .describe("Brain source IDs the grounding paragraph cites — used to wire 'quantifies' edges"),
      }),
      handler: async (
        { question, outcomes, grounding_context, model_type, cited_source_ids },
        api
      ) => {
        const created = await createModel({
          question,
          outcomes,
          additionalContext: grounding_context,
          modelType: model_type,
        });

        if (created.error) {
          return { error: created.error, status_code: created.status_code };
        }

        const apiModel = Array.isArray(created) ? created[0] : created;
        const platformModelId = apiModel?.id || apiModel?.modelId || null;
        if (!platformModelId) {
          return { error: "51Folds API returned no model id", api_response: apiModel };
        }

        const nodeId = await nextPredictionGraphId(api);
        const predDomainGroup = await findOrSeedPredictionDomain(api);
        const now = new Date().toISOString();

        await api.predictionSave(platformModelId, question, {
          model_type,
          status: "queued",
          outcomes,
          source_thesis: grounding_context.slice(0, 1000),
          graph_node_id: nodeId,
          platform_model_id: platformModelId,
          created_at: now,
        });

        await api.addNode(nodeId, question, "prediction", predDomainGroup, {
          model_type,
          platform_model_id: platformModelId,
          status: "queued",
          created_at: now,
        });

        const wired = [];
        for (const sid of cited_source_ids || []) {
          try {
            const r = await api.addEdge(nodeId, sid, "quantifies", "grounding citation");
            if (r.inserted) wired.push(sid);
          } catch {
            /* skip on conflict */
          }
        }

        await api.exportGraph();

        return {
          prediction_id: nodeId,
          platform_model_id: platformModelId,
          question,
          outcomes,
          model_type,
          status: "queued",
          wired_to: wired,
        };
      },
    },

    // ------------------------------------------------------------------------
    // prism_folds_status (declared as bare 'status')
    // ------------------------------------------------------------------------
    {
      name: "status",
      description:
        "Check progress and status for one prediction model (or all registered prediction models). Polls the 51Folds platform API. When status indicates completion, the user (or you) should call prism_folds_ingest_results.",
      inputSchema: z.object({
        prediction_id: z
          .string()
          .optional()
          .describe("Local prediction id (e.g. 'P01') or platform_model_id. Omit for all."),
      }),
      handler: async ({ prediction_id }, api) => {
        const all = await api.predictionList();
        if (!all || all.length === 0) {
          return { count: 0, predictions: [] };
        }

        let target = all;
        if (prediction_id) {
          target = all.filter(
            (p) => p.graph_node_id === prediction_id || p.model_id === prediction_id
          );
          if (target.length === 0) return { error: `Prediction ${prediction_id} not found` };
        }

        const out = [];
        for (const p of target) {
          const live = await getModelProgress(p.model_id);
          if (live.error) {
            out.push({
              prediction_id: p.graph_node_id,
              platform_model_id: p.model_id,
              question: p.question,
              local_status: p.status,
              poll_error: live.error,
            });
            continue;
          }
          await api.predictionUpdate(p.model_id, {
            status: live.statusLabel?.toLowerCase() || p.status,
            progress: live.progress,
            status_label: live.statusLabel,
            short_summary: live.shortSummary,
          });
          out.push({
            prediction_id: p.graph_node_id,
            platform_model_id: p.model_id,
            question: p.question,
            progress: live.progress,
            status: live.status,
            status_label: live.statusLabel,
            short_summary: live.shortSummary,
            ready_for_ingest: live.status === 3 || /complete/i.test(live.statusLabel || ""),
          });
        }
        return { count: out.length, predictions: out };
      },
    },

    // ------------------------------------------------------------------------
    // prism_folds_ingest_results (declared as bare 'ingest_results')
    // ------------------------------------------------------------------------
    {
      name: "ingest_results",
      description:
        "Fetch completed model results, draft a narrative summary as a new brain source, and wire 'quantifies' edges from the prediction node to related concepts found via search. Use after prism_folds_status reports completion.",
      inputSchema: z.object({
        prediction_id: z
          .string()
          .describe("Local prediction id (e.g. 'P01') or platform_model_id"),
      }),
      handler: async ({ prediction_id }, api) => {
        const all = await api.predictionList();
        const pred = (all || []).find(
          (p) => p.graph_node_id === prediction_id || p.model_id === prediction_id
        );
        if (!pred) return { error: `Prediction ${prediction_id} not found` };

        const results = await getModelResults(pred.model_id);
        if (results.error) return { error: results.error };

        const reportResp = await getReport(pred.model_id);
        const reportText = !reportResp.error
          ? (Array.isArray(reportResp) ? reportResp[0]?.content : reportResp.content) || ""
          : "";

        const narrative_lines = [
          `# 51Folds Model: ${pred.question}`,
          "",
          `**Model id:** ${pred.model_id}`,
          `**Status:** ${results.statusLabel}`,
          "",
          "## Short summary",
          "",
          results.shortSummary || "_(none)_",
          "",
        ];

        if (results.result?.dv) {
          narrative_lines.push("## Outcome probabilities", "");
          for (const [outcome, prob] of Object.entries(results.result.dv || {})) {
            narrative_lines.push(`- **${outcome}**: ${(prob * 100).toFixed(1)}%`);
          }
          narrative_lines.push("");
        }

        if (results.justification) {
          narrative_lines.push("## Justification", "", results.justification, "");
        }

        if (reportText) {
          narrative_lines.push("## Executive summary report", "", reportText, "");
        }

        const narrative = narrative_lines.join("\n");

        const ingested = await api.ingestText(
          narrative,
          `51Folds: ${pred.question}`,
          { source_kind: "51folds-model", platform_model_id: pred.model_id }
        );

        const wired = [];
        const searchResults = await api.search(pred.question, 5);
        for (const r of searchResults || []) {
          try {
            const added = await api.addEdge(
              pred.graph_node_id,
              r.source_id,
              "quantifies",
              `relevance: ${(r.score || 0).toFixed(3)}`
            );
            if (added.inserted) wired.push(r.source_id);
          } catch {
            /* skip */
          }
        }

        await api.predictionUpdate(pred.model_id, {
          status: "completed",
          progress: 100,
          short_summary: results.shortSummary,
          ingested_to_brain: true,
          added_to_graph: true,
          completed_at: new Date().toISOString(),
        });

        await api.exportGraph();

        return {
          prediction_id: pred.graph_node_id,
          platform_model_id: pred.model_id,
          ingested_source: ingested.source_id,
          wired_to: wired,
          short_summary: results.shortSummary,
        };
      },
    },
  ];
}

// Exposed for direct import in tests / debugging.
export { checkCredits };
