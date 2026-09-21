import { afterEach, expect, it, vi } from 'vitest';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  vi.resetModules();
});

it('forwards request cancellation without turning it into a connection error', async () => {
  const controller = new AbortController();
  const abort = new DOMException('Aborted', 'AbortError');
  const fetch = vi.fn().mockRejectedValue(abort);
  vi.stubGlobal('fetch', fetch);
  const { providersApi } = await import('./api');
  controller.abort();
  await expect(providersApi.list(controller.signal)).rejects.toBe(abort);
  expect(fetch).toHaveBeenCalledWith(
    '/api/v1/providers',
    expect.objectContaining({ signal: controller.signal }),
  );
});

it('removes the launch credential from the URL and distinguishes a cloned tab controller', async () => {
  vi.useFakeTimers();
  sessionStorage.setItem('mclaw.connection.client', 'cloned-controller');
  window.history.replaceState(null, '', '/#token=test-token-only');
  class Channel {
    onmessage: ((event: { data: unknown }) => void) | null = null;
    postMessage(message: { type: string; client: string; instance: string }) {
      if (message.type === 'probe')
        this.onmessage?.({
          data: {
            type: 'occupied',
            client: message.client,
            instance: 'other-live-page',
            target: message.instance,
          },
        });
    }
    close() {}
  }
  vi.stubGlobal('BroadcastChannel', Channel);
  const { initializeConnection } = await import('./api');
  const ready = initializeConnection();
  await vi.advanceTimersByTimeAsync(181);
  await ready;
  expect(window.location.hash).toBe('');
  expect(sessionStorage.getItem('mclaw.connection.token')).toBe('test-token-only');
  expect(sessionStorage.getItem('mclaw.connection.client')).not.toBe('cloned-controller');
});

it('keeps a refresh controller when there is no live collision', async () => {
  vi.useFakeTimers();
  sessionStorage.setItem('mclaw.connection.client', 'refresh-controller');
  vi.stubGlobal(
    'BroadcastChannel',
    class {
      onmessage = null;
      postMessage() {}
      close() {}
    },
  );
  const { initializeConnection } = await import('./api');
  const ready = initializeConnection();
  await vi.advanceTimersByTimeAsync(181);
  await ready;
  expect(sessionStorage.getItem('mclaw.connection.client')).toBe('refresh-controller');
});

it('sends authentication only as a header for HTTP and surfaces structured errors', async () => {
  const fetch = vi.fn().mockResolvedValue({
    ok: false,
    status: 409,
    json: async () => ({ error: { code: 'SESSION_OCCUPIED', message: '由另一个入口控制' } }),
  });
  vi.stubGlobal('fetch', fetch);
  sessionStorage.setItem('mclaw.connection.token', 'test-only-token');
  sessionStorage.setItem('mclaw.connection.client', 'test-client');
  const { api } = await import('./api');
  await expect(api.session('session/a')).rejects.toMatchObject({
    code: 'SESSION_OCCUPIED',
    status: 409,
    message: '由另一个入口控制',
  });
  expect(fetch).toHaveBeenCalledWith(
    '/api/v1/sessions/session%2Fa',
    expect.objectContaining({
      headers: { Authorization: 'Bearer test-only-token', 'X-MClaw-Client': 'test-client' },
    }),
  );
});

it('uploads raw audio with authentication and duration instead of serializing the recording', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ text: '转写' }) });
  vi.stubGlobal('fetch', fetch);
  sessionStorage.setItem('mclaw.connection.token', 'test-only-token');
  sessionStorage.setItem('mclaw.connection.client', 'test-client');
  const { voiceApi } = await import('./api');
  const audio = new Blob(['recording'], { type: 'audio/webm' });
  expect(await voiceApi.transcribe(audio, 4000)).toEqual({ text: '转写' });
  expect(fetch).toHaveBeenCalledWith(
    '/api/v1/voice/transcribe',
    expect.objectContaining({
      body: audio,
      headers: {
        Authorization: 'Bearer test-only-token',
        'X-MClaw-Client': 'test-client',
        'Content-Type': 'audio/webm',
        'X-Audio-Duration-Ms': '4000',
      },
    }),
  );
});
