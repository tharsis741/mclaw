import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { filesApi } from '../api';
import { AttachmentPicker, FilePreviewPanel } from './Attachments';
import type { FilePreview, FileReference } from '../types';

vi.mock('../api', () => ({
  filesApi: { upload: vi.fn(), link: vi.fn(), browse: vi.fn(), preview: vi.fn(), content: vi.fn() },
}));
const file: FileReference = {
  id: 'file-1',
  name: 'notes.md',
  path: 'C:/workspace/notes.md',
  source: 'upload',
  size: 20,
  modified_at: 1,
  created_at: 1,
  device: '本机',
  workspace: 'C:/workspace',
  mime_type: 'text/markdown',
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(filesApi.upload).mockResolvedValue(file);
});

it('dismisses the attachment picker outside and with Escape without attaching a file', async () => {
  const added = vi.fn();
  render(
    <>
      <AttachmentPicker
        ensureSession={async () => 's'}
        onAdded={added}
        count={0}
        disabled={false}
        labelled
      />
      <button>输入区</button>
    </>,
  );
  const trigger = screen.getByRole('button', { name: '添加附件' });
  await userEvent.click(trigger);
  expect(await screen.findByRole('dialog')).toBeVisible();
  await userEvent.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(trigger).toHaveFocus();
  await userEvent.click(trigger);
  await screen.findByRole('dialog');
  await userEvent.click(screen.getByRole('button', { name: '输入区' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('button', { name: '输入区' })).toHaveFocus();
  expect(added).not.toHaveBeenCalled();
});

it('restores focus after filling the attachment allowance and shows the full state', async () => {
  function FullPicker() {
    const [count, setCount] = useState(19);
    return (
      <AttachmentPicker
        ensureSession={async () => 's'}
        onAdded={() => setCount((value) => value + 1)}
        count={count}
        disabled={false}
      />
    );
  }
  render(<FullPicker />);
  const trigger = screen.getByRole('button', { name: '添加附件' });
  await userEvent.click(trigger);
  await userEvent.upload(
    await screen.findByLabelText('选择上传文件'),
    new File(['notes'], 'notes.txt'),
  );
  expect(trigger).toHaveFocus();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await userEvent.keyboard('{Enter}');
  expect(await screen.findByText(/还可添加 0 个/)).toBeVisible();
  expect(screen.getByRole('button', { name: '选择文件 单个文件最大 20 MB' })).toBeDisabled();
  expect(screen.getByLabelText('选择上传文件')).toBeDisabled();
  expect(screen.getByRole('button', { name: '关闭附件选择' })).toHaveFocus();
});

it('binds uploaded files to the created session and retains successful additions if another upload fails', async () => {
  vi.mocked(filesApi.upload)
    .mockResolvedValueOnce(file)
    .mockRejectedValueOnce(new Error('文件不可保存'));
  const added = vi.fn();
  const ensure = vi.fn().mockResolvedValue('session-a');
  render(<AttachmentPicker ensureSession={ensure} onAdded={added} count={0} disabled={false} />);
  expect(ensure).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '添加附件' }));
  await screen.findByRole('dialog');
  await userEvent.upload(screen.getByLabelText('选择上传文件'), [
    new File(['notes'], 'notes.md'),
    new File(['other'], 'other.md'),
  ]);
  await screen.findByRole('alert');
  expect(added).toHaveBeenCalledWith('session-a', file);
  expect(screen.getByRole('alert')).toHaveTextContent('文件不可保存');
  expect(filesApi.upload).toHaveBeenNthCalledWith(1, 'session-a', expect.any(File));
});

it('limits a batch to eight images while still accepting text attachments', async () => {
  vi.mocked(filesApi.upload).mockImplementation(async (_session, uploaded) => ({
    ...file,
    id: uploaded.name,
    name: uploaded.name,
    mime_type: uploaded.name.endsWith('.png') ? 'image/png' : 'text/plain',
  }));
  const added = vi.fn();
  render(
    <AttachmentPicker ensureSession={async () => 's'} onAdded={added} count={0} disabled={false} />,
  );
  await userEvent.click(screen.getByRole('button', { name: '添加附件' }));
  await userEvent.upload(await screen.findByLabelText('选择上传文件'), [
    ...Array.from({ length: 9 }, (_, i) => new File(['image'], `${i}.png`)),
    new File(['notes'], 'notes.txt'),
  ]);
  expect(await screen.findByRole('alert')).toHaveTextContent('8.png：每条消息最多添加 8 张图片');
  expect(filesApi.upload).toHaveBeenCalledTimes(9);
  expect(added).toHaveBeenCalledTimes(9);
  expect(added.mock.calls.at(-1)?.[1].name).toBe('notes.txt');
  // A second selection in the same open picker shares the remaining allowance.
  await userEvent.upload(screen.getByLabelText('选择上传文件'), new File(['image'], 'extra.png'));
  expect(added).toHaveBeenCalledTimes(9);
});

it('counts existing images and lets removal free a slot without blocking text files', async () => {
  vi.mocked(filesApi.upload).mockImplementation(async (_session, uploaded) => ({
    ...file,
    id: uploaded.name,
    name: uploaded.name,
    mime_type: uploaded.type,
  }));
  const added = vi.fn();
  const props = { ensureSession: async () => 's', onAdded: added, disabled: false };
  const view = render(<AttachmentPicker {...props} count={8} imageCount={8} />);
  await userEvent.click(screen.getByRole('button', { name: '添加附件' }));
  await userEvent.upload(
    await screen.findByLabelText('选择上传文件'),
    new File(['image'], 'extra.png', { type: 'image/png' }),
  );
  expect(await screen.findByRole('alert')).toHaveTextContent('最多添加 8 张图片');
  expect(filesApi.upload).not.toHaveBeenCalled();
  view.rerender(<AttachmentPicker {...props} count={7} imageCount={7} />);
  await userEvent.upload(screen.getByLabelText('选择上传文件'), [
    new File(['image'], 'extra.png', { type: 'image/png' }),
    new File(['notes'], 'notes.txt', { type: 'text/plain' }),
  ]);
  expect(added).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '添加附件' })).toHaveFocus();
});

it('enforces the image allowance for local references', async () => {
  vi.mocked(filesApi.browse).mockResolvedValue({
    path: 'C:/workspace',
    parent: 'C:/',
    truncated: false,
    max_upload_bytes: 20000000,
    entries: [{ name: 'image.png', path: 'C:/workspace/image.png', directory: false, size: 10 }],
  });
  const added = vi.fn();
  render(
    <AttachmentPicker
      ensureSession={async () => 's'}
      onAdded={added}
      count={8}
      imageCount={8}
      disabled={false}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: '添加附件' }));
  await userEvent.click(await screen.findByRole('button', { name: '引用本机文件' }));
  await userEvent.click(await screen.findByRole('button', { name: /image.png/ }));
  expect(await screen.findByRole('alert')).toHaveTextContent('最多添加 8 张图片');
  expect(added).not.toHaveBeenCalled();
  expect(filesApi.link).not.toHaveBeenCalled();
});

it('reads a local directory and references the original file only after selection', async () => {
  vi.mocked(filesApi.browse).mockResolvedValue({
    path: 'C:/workspace',
    parent: 'C:/',
    entries: [{ name: file.name, path: file.path, directory: false, size: file.size }],
    truncated: false,
    max_upload_bytes: 20000000,
  });
  vi.mocked(filesApi.link).mockResolvedValue({ ...file, source: 'local' });
  const added = vi.fn();
  render(
    <AttachmentPicker ensureSession={async () => 's'} onAdded={added} count={0} disabled={false} />,
  );
  await userEvent.click(screen.getByRole('button', { name: '添加附件' }));
  await userEvent.click(await screen.findByRole('button', { name: '引用本机文件' }));
  expect(filesApi.link).not.toHaveBeenCalled();
  await userEvent.click(await screen.findByRole('button', { name: /notes.md/ }));
  await waitFor(() => expect(filesApi.link).toHaveBeenCalledWith('s', 'C:/workspace/notes.md'));
  expect(added).toHaveBeenCalledWith('s', expect.objectContaining({ source: 'local' }));
});

it('renders markdown as safe content and revokes a binary preview URL on close', async () => {
  vi.mocked(filesApi.preview).mockResolvedValueOnce({
    file,
    kind: 'markdown',
    content: '# 文件正文\n<script>bad()</script>',
  });
  const view = render(<FilePreviewPanel session="s" file={file} onClose={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: '文件正文' })).toBeVisible();
  expect(document.querySelector('script')).toBeNull();
  view.unmount();
  const create = vi.fn().mockReturnValue('blob:preview');
  const revoke = vi.fn();
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }));
  vi.mocked(filesApi.preview).mockResolvedValueOnce({
    file: { ...file, name: 'picture.png', mime_type: 'image/png' },
    kind: 'image',
  });
  vi.mocked(filesApi.content).mockResolvedValue(new Blob(['bytes']));
  const imageView = render(<FilePreviewPanel session="s" file={file} onClose={vi.fn()} />);
  expect(await screen.findByRole('img')).toHaveAttribute('src', 'blob:preview');
  expect(filesApi.content).toHaveBeenCalledWith('s', file.id);
  imageView.unmount();
  expect(revoke).toHaveBeenCalledWith('blob:preview');
});

function PreviewExample() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>预览附件</button>
      <button>移除附件</button>
      {open && <FilePreviewPanel session="s" file={file} onClose={() => setOpen(false)} />}
    </>
  );
}

it('focuses the preview and cycles Tab through its controls, including details and markdown links', async () => {
  const user = userEvent.setup();
  vi.mocked(filesApi.preview).mockResolvedValueOnce({
    file,
    kind: 'markdown',
    content: '[参考资料](https://example.com)',
  });
  render(<PreviewExample />);
  const trigger = screen.getByRole('button', { name: '预览附件' });
  await user.click(trigger);
  const close = screen.getByRole('button', { name: '关闭文件预览' });
  const link = await screen.findByRole('link', { name: '参考资料' });
  expect(close).toHaveFocus();
  await user.tab({ shift: true });
  expect(link).toHaveFocus();
  await user.tab();
  expect(close).toHaveFocus();
  await user.tab();
  expect(screen.getByRole('button', { name: '下载' })).toHaveFocus();
  await user.tab();
  expect(screen.getByText('文件位置')).toHaveFocus();
  await user.tab();
  expect(link).toHaveFocus();
  await user.tab();
  expect(close).toHaveFocus();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it('keeps focus inside while loading and does not steal focus when preview data arrives', async () => {
  const user = userEvent.setup();
  let resolve!: (value: FilePreview) => void;
  vi.mocked(filesApi.preview).mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  render(<PreviewExample />);
  await user.click(screen.getByRole('button', { name: '预览附件' }));
  const close = screen.getByRole('button', { name: '关闭文件预览' });
  expect(screen.getByText('正在读取文件…')).toBeVisible();
  await user.tab();
  expect(close).toHaveFocus();
  await user.tab({ shift: true });
  expect(close).toHaveFocus();
  await act(async () => resolve({ file, kind: 'text', content: '正文' }));
  expect(close).toHaveFocus();
  await user.tab();
  expect(screen.getByRole('button', { name: '下载' })).toHaveFocus();
  await user.tab();
  expect(screen.getByText('文件位置')).toHaveFocus();
  await user.tab();
  expect(close).toHaveFocus();
});

it('ignores composition Escape and stays dismissible after a preview error', async () => {
  const user = userEvent.setup();
  vi.mocked(filesApi.preview).mockRejectedValueOnce(new Error('文件不可用'));
  render(<PreviewExample />);
  const trigger = screen.getByRole('button', { name: '预览附件' });
  await user.click(trigger);
  expect(await screen.findByRole('alert')).toHaveTextContent('文件不可用');
  fireEvent.keyDown(screen.getByRole('button', { name: '关闭文件预览' }), {
    key: 'Escape',
    isComposing: true,
  });
  expect(screen.getByRole('dialog')).toBeVisible();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it.each(['button', 'backdrop', 'cancel'])(
  'restores attachment focus when closed by %s',
  async (method) => {
    const user = userEvent.setup();
    vi.mocked(filesApi.preview).mockResolvedValueOnce({ file, kind: 'text', content: '正文' });
    render(<PreviewExample />);
    const trigger = screen.getByRole('button', { name: '预览附件' });
    await user.click(trigger);
    const dialog = screen.getByRole('dialog');
    if (method === 'button') await user.click(screen.getByRole('button', { name: '关闭文件预览' }));
    else if (method === 'backdrop') fireEvent.click(dialog);
    else fireEvent(dialog, new Event('cancel', { cancelable: true }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  },
);
