import { afterEach, describe, expect, it } from "bun:test";
import { volcengineCodingPlanModelManagerOptions } from "@oh-my-pi/pi-ai/provider-models/openai-compat";
import { detectOpenAICompat, resolveOpenAICompat } from "@oh-my-pi/pi-ai/providers/openai-completions-compat";
import type { Model } from "@oh-my-pi/pi-ai/types";

/**
 * Resolver-branch coverage for the `isVolcengine` path added by the
 * `volcengine-coding-plan` provider. Mirrors zhipu-compat.test.ts: assert the
 * contract the provider relies on (zai thinking format, disabled
 * `reasoning_effort`, no `developer` role) so future refactors of
 * `detectOpenAICompat` cannot silently regress the SKUs. Discovery is a
 * multi-vendor gateway (Doubao, MiniMax, GLM, DeepSeek, Kimi), so capabilities
 * flow from the bundled reference catalog, not id heuristics.
 */

const CODING_BASE = "https://ark.cn-beijing.volces.com/api/coding/v3";

const baseModel: Omit<Model<"openai-completions">, "provider" | "baseUrl"> = {
	api: "openai-completions",
	id: "doubao-seed-2.0-pro",
	name: "Doubao Seed 2.0 Pro",
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	maxTokens: 32_000,
	contextWindow: 256_000,
	reasoning: true,
};

const originalFetch = global.fetch;

afterEach(() => {
	global.fetch = originalFetch;
});

function volcengineByProvider(): Model<"openai-completions"> {
	return {
		...baseModel,
		provider: "volcengine-coding-plan",
		baseUrl: CODING_BASE,
	};
}

function volcengineByBaseUrl(): Model<"openai-completions"> {
	return {
		...baseModel,
		// Provider intentionally not "volcengine-coding-plan" — exercises the
		// URL-based fallback branch.
		provider: "custom",
		baseUrl: `${CODING_BASE}/chat/completions`,
	};
}

describe("openai-completions compat — volcengine-coding-plan branch", () => {
	it("forces zai thinking format and disables reasoning_effort / developer role", () => {
		const compat = detectOpenAICompat(volcengineByProvider());

		expect(compat.thinkingFormat).toBe("zai");
		expect(compat.supportsReasoningEffort).toBe(false);
		expect(compat.supportsDeveloperRole).toBe(false);
		expect(compat.reasoningContentField).toBe("reasoning_content");
		// Volcengine is not on the multi-system-message allowlist, so it uses the
		// conservative default (messages get merged before sending). Flip this to
		// `true` only once Ark is confirmed to accept multiple system messages.
		expect(compat.supportsMultipleSystemMessages).toBe(false);
		// `isVolcengine` participates in the non-standard set, so `store` is off.
		expect(compat.supportsStore).toBe(false);
	});

	it("detects volcengine by baseUrl when provider id is custom", () => {
		const compat = detectOpenAICompat(volcengineByBaseUrl());

		expect(compat.thinkingFormat).toBe("zai");
		expect(compat.supportsReasoningEffort).toBe(false);
	});

	it("lets explicit model.compat overrides win at the resolver layer", () => {
		const model: Model<"openai-completions"> = {
			...volcengineByProvider(),
			compat: {
				supportsDeveloperRole: true,
				supportsReasoningEffort: true,
				thinkingFormat: "openai",
			},
		};
		const resolved = resolveOpenAICompat(model);

		expect(resolved.supportsDeveloperRole).toBe(true);
		expect(resolved.supportsReasoningEffort).toBe(true);
		expect(resolved.thinkingFormat).toBe("openai");
		// Untouched fields still come from the volcengine branch.
		expect(resolved.reasoningContentField).toBe("reasoning_content");
	});
});

describe("volcengine-coding-plan model discovery", () => {
	function mockFetchReturning(ids: string[]): () => string {
		let requestedUrl = "";
		const mockFetch = async (input: string | Request | URL): Promise<Response> => {
			requestedUrl = input instanceof Request ? input.url : String(input);
			return new Response(JSON.stringify({ data: ids.map(id => ({ id })) }), {
				headers: { "content-type": "application/json" },
			});
		};
		global.fetch = Object.assign(mockFetch, { preconnect: originalFetch.preconnect });
		return () => requestedUrl;
	}

	it("hits the coding-plan /models endpoint and surfaces bundled capabilities", async () => {
		const getUrl = mockFetchReturning(["doubao-seed-2.0-pro", "deepseek-v4-pro"]);

		const options = volcengineCodingPlanModelManagerOptions({ apiKey: "test-key" });
		expect(typeof options.fetchDynamicModels).toBe("function");
		const models = await options.fetchDynamicModels?.();
		const byId = new Map(models?.map(m => [m.id, m]));

		expect(getUrl()).toBe(`${CODING_BASE}/models`);

		// Multimodal Doubao reasoning SKU — caps come from the bundled reference.
		const doubao = byId.get("doubao-seed-2.0-pro");
		expect(doubao?.baseUrl).toBe(CODING_BASE);
		expect(doubao?.reasoning).toBe(true);
		expect(doubao?.input).toEqual(["text", "image"]);

		// Non-Doubao vendor on the same gateway — text-only reasoning model. The old
		// id-`seed` heuristic would have wrongly marked this `reasoning: false`.
		const deepseek = byId.get("deepseek-v4-pro");
		expect(deepseek?.reasoning).toBe(true);
		expect(deepseek?.input).toEqual(["text"]);
	});

	it("applies the zai compat contract to discovered models at resolve time", async () => {
		mockFetchReturning(["minimax-latest"]);

		const models = await volcengineCodingPlanModelManagerOptions({ apiKey: "k" }).fetchDynamicModels?.();
		const model = models?.[0];
		expect(model?.provider).toBe("volcengine-coding-plan");

		// mapModel does not stamp compat; the zai contract is applied by the runtime
		// isVolcengine detection in resolveOpenAICompat.
		const resolved = model && resolveOpenAICompat(model);
		expect(resolved?.thinkingFormat).toBe("zai");
		expect(resolved?.supportsReasoningEffort).toBe(false);
		expect(resolved?.supportsDeveloperRole).toBe(false);
		expect(resolved?.reasoningContentField).toBe("reasoning_content");
	});
});
