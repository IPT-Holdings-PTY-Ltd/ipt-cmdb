import { describe, expect, it, vi } from 'vitest';
import { apiFetch } from './session';

describe('API retry metadata', () => {
  it('preserves a numeric Retry-After header on structured errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ detail: 'Too many sign-in attempts. Try again later.' }),
          {
            status: 429,
            headers: {
              'content-type': 'application/json',
              'retry-after': '73',
            },
          },
        ),
      ),
    );

    await expect(
      apiFetch('/api/login', { method: 'POST', body: '{}' }),
    ).rejects.toMatchObject({
      name: 'ApiError',
      status: 429,
      retryAfterSeconds: 73,
    });
    vi.unstubAllGlobals();
  });
});
