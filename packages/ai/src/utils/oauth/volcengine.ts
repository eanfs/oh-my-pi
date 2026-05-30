/**
 * Volcengine Coding Plan login flow.
 *
 * Volcengine Ark Coding Plan exposes an OpenAI-compatible, multi-vendor catalog
 * (Doubao, MiniMax, GLM, DeepSeek, Kimi) via the coding endpoint
 * https://ark.cn-beijing.volces.com/api/coding/v3.
 *
 * This is not OAuth — it's a simple API key paste-and-validate flow:
 * open the Ark console, paste the key, validate it against `/models`.
 *
 * Validation hits `GET /api/coding/v3/models` rather than a chat completion so
 * it authenticates the key without depending on a specific model SKU being
 * enabled on the account (model IDs and entitlements vary per account).
 */

import { createApiKeyLogin } from "./api-key-login";

export const loginVolcengineCodingPlan = createApiKeyLogin({
	providerLabel: "Volcengine Coding Plan",
	authUrl: "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
	instructions: "Copy your API key from the Volcengine Ark console",
	promptMessage: "Paste your Volcengine Ark API key",
	placeholder: "sk-...",
	validation: {
		kind: "models-endpoint",
		provider: "Volcengine Coding Plan",
		modelsUrl: "https://ark.cn-beijing.volces.com/api/coding/v3/models",
	},
});
