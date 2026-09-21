import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { filesApi } from '../api';
import { AttachmentPicker, FilePreviewPanel } from './Attachments';
import type { FileReference } from '../types';

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
  expect(added).not.toHaveBeenCalled();
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
