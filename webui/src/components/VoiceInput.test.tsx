import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { voiceApi } from '../api';
import { VoiceInput } from './VoiceInput';

vi.mock('../api', () => ({
  voiceApi: { status: vi.fn(), configure: vi.fn(), transcribe: vi.fn() },
}));
const status = {
  enabled: true,
  ready: true,
  credential_configured: true,
  authorized: true,
  provider: 'qwen' as const,
  region: 'cn' as const,
  model: 'qwen3-asr-flash',
  max_seconds: 120,
  max_bytes: 7340032,
  mime_types: ['audio/webm'],
  notice: '录音将发送至配置的 Qwen 服务进行转写。',
};
const stopTrack = vi.fn();
afterEach(() => vi.useRealTimers());
const getUserMedia = vi.fn();
class FakeRecorder {
  static isTypeSupported() {
    return true;
  }
  state = 'inactive';
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror = null;
  start() {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({ data: new Blob(['recording']) });
    this.onstop?.();
  }
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
  getUserMedia.mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] });
  vi.mocked(voiceApi.status).mockResolvedValue(status);
  vi.mocked(voiceApi.transcribe).mockResolvedValue({ text: '转写文字' });
});

it('requests microphone only on click and routes transcription to the original draft', async () => {
  let resolve!: (value: { text: string }) => void;
  vi.mocked(voiceApi.transcribe).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const onTranscript = vi.fn();
  const view = render(
    <VoiceInput draftKey="session-a" disabled={false} onTranscript={onTranscript} />,
  );
  expect(getUserMedia).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '语音输入' }));
  await screen.findByRole('button', { name: '结束录音并转写' });
  view.rerender(<VoiceInput draftKey="session-b" disabled={false} onTranscript={onTranscript} />);
  await userEvent.click(screen.getByRole('button', { name: '结束录音并转写' }));
  expect(stopTrack).toHaveBeenCalledOnce();
  expect(voiceApi.transcribe).toHaveBeenCalledWith(expect.any(Blob), expect.any(Number));
  await act(async () => resolve({ text: '写回原会话' }));
  expect(onTranscript).toHaveBeenCalledWith('session-a', '写回原会话');
});

it('automatically stops at the backend duration limit and transcribes once', async () => {
  vi.useFakeTimers();
  render(<VoiceInput draftKey="s" disabled={false} onTranscript={vi.fn()} />);
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: '语音输入' }));
  });
  expect(getUserMedia).toHaveBeenCalledOnce();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(120000);
  });
  expect(voiceApi.transcribe).toHaveBeenCalledWith(expect.any(Blob), 120000);
  expect(voiceApi.transcribe).toHaveBeenCalledOnce();
  expect(stopTrack).toHaveBeenCalledOnce();
});

it('configures a scoped key before asking for recording permission', async () => {
  vi.mocked(voiceApi.status).mockResolvedValue({
    ...status,
    ready: false,
    credential_configured: false,
  });
  vi.mocked(voiceApi.configure).mockResolvedValue(status);
  render(<VoiceInput draftKey="new" disabled={false} onTranscript={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: '语音输入' }));
  await screen.findByRole('dialog');
  expect(getUserMedia).not.toHaveBeenCalled();
  await userEvent.type(screen.getByLabelText('Qwen API Key'), 'test-key');
  await userEvent.click(screen.getByRole('button', { name: '保存并开启' }));
  await waitFor(() =>
    expect(voiceApi.configure).toHaveBeenCalledWith({
      enabled: true,
      provider: 'qwen',
      api_key: 'test-key',
    }),
  );
  expect(getUserMedia).not.toHaveBeenCalled();
});

it('preserves the draft on transcription failure and releases the microphone on unmount', async () => {
  vi.mocked(voiceApi.transcribe).mockRejectedValue(new Error('转写服务不可用'));
  const onTranscript = vi.fn();
  const view = render(<VoiceInput draftKey="a" disabled={false} onTranscript={onTranscript} />);
  await userEvent.click(screen.getByRole('button', { name: '语音输入' }));
  await userEvent.click(await screen.findByRole('button', { name: '结束录音并转写' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('转写服务不可用');
  expect(onTranscript).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '语音输入' }));
  await screen.findByRole('button', { name: '结束录音并转写' });
  view.unmount();
  expect(stopTrack).toHaveBeenCalledTimes(2);
  expect(voiceApi.transcribe).toHaveBeenCalledOnce();
});
