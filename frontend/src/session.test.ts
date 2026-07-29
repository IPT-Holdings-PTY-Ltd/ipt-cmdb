import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiDownload, apiFetch } from './session';

describe('API requests', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('preserves a numeric Retry-After header on structured errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(
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
  });

  it('adds JSON content type only for valid serialized JSON', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await apiFetch('/api/example', { method: 'POST', body: '{}' });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get('content-type')).toBe('application/json');
  });

  it.each([
    ['plain text', 'not-json'],
    ['form data', new FormData()],
    ['URL parameters', new URLSearchParams({ query: 'value' })],
    ['binary data', new Blob(['content'])],
  ])('does not label %s request bodies as JSON', async (_label, body) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await apiFetch('/api/example', { method: 'POST', body });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).has('content-type')).toBe(false);
  });

  it('preserves explicit Headers instances and content types', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await apiFetch('/api/example', {
      method: 'POST',
      body: '{}',
      headers: new Headers({
        'content-type': 'application/problem+json',
        'x-request-purpose': 'test',
      }),
    });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get('content-type')).toBe('application/problem+json');
    expect(headers.get('x-request-purpose')).toBe('test');
  });

  it('uses the same body-aware header behavior for downloads', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(new Blob(['content']), {
        status: 200,
        headers: { 'content-disposition': 'attachment; filename="result.bin"' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const result = await apiDownload('/api/export', {
      method: 'POST',
      body: new Blob(['request']),
    });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).has('content-type')).toBe(false);
    expect(result.filename).toBe('result.bin');
  });
});
