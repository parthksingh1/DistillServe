import type { LivenessResponse, ReadinessResponse } from './types';

/**
 * Every backend call goes through this module.
 *
 * In development Vite proxies `/api/*` to the gateway; in production Vercel
 * rewrites the same prefix to the Render service. So the base URL is a
 * constant, and no component ever needs to know where the gateway lives.
 */
const API_PREFIX = '/api';

export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'GatewayError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    headers: { Accept: 'application/json' },
    ...init,
  });

  // `/readyz` answers 503 with a full, useful body when a dependency is down,
  // so the body is parsed before the status is judged.
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok && response.status !== 503) {
    throw new GatewayError(`${path} failed with ${String(response.status)}`, response.status);
  }
  if (body === null) {
    throw new GatewayError(`${path} returned a non-JSON body`, response.status);
  }
  return body as T;
}

export const gateway = {
  health: () => request<LivenessResponse>('/healthz'),
  readiness: () => request<ReadinessResponse>('/readyz'),
};
