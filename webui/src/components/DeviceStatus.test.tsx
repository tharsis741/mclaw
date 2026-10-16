import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { devicesApi } from '../api';
import type { Bootstrap, DeviceSnapshot } from '../types';
import { DeviceStatus } from './SettingsPage';

vi.mock('../api', async () => ({
  ...(await vi.importActual('../api')),
  devicesApi: { status: vi.fn(), configure: vi.fn(), act: vi.fn() },
}));

const device = {
  deviceIdSha256: 'a'.repeat(64),
  deviceName: '开鸿电脑 B',
  deviceTypeId: 12,
  publicDeviceId: 'urn:mclaw:device:oh:' + 'a'.repeat(64),
};
const ready: DeviceSnapshot = {
  supported: true,
  enabled: true,
  state: 'ready',
  provider_ready: true,
  error: null,
  operation: null,
  localDevice: null,
  devices: [],
  trustedDevices: [],
  pairableDevices: [],
  discoveryWarning: null,
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(devicesApi.status).mockResolvedValue(ready);
  vi.mocked(devicesApi.act).mockResolvedValue(ready);
});

const bootstrap: Bootstrap = {
  setup_required: false,
  version: 'test',
  device: { name: '测试设备', platform: 'windows' },
  models: [],
  capabilities: [
    { id: 'pet', label: '桌面伙伴', available: true },
    { id: 'dsoftbus', label: 'dsoftbus', available: true },
  ],
};

it('shows the Windows collaboration boundary and device information without a diagnostic dump', () => {
  render(<DeviceStatus bootstrap={bootstrap} />);
  expect(screen.getByText('跨设备 M-Claw 协同不可用，仅支持 Kaihong OS 端。')).toBeVisible();
  expect(screen.getByText('Windows')).toBeVisible();
  expect(screen.getByText('测试设备')).toBeVisible();
  expect(screen.queryByText('桌面伙伴')).not.toBeInTheDocument();
  expect(screen.queryByText('dsoftbus')).not.toBeInTheDocument();
  expect(devicesApi.status).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: '搜索设备' })).not.toBeInTheDocument();
});

it('searches on request and requires confirmation before system pairing', async () => {
  vi.mocked(devicesApi.act).mockResolvedValue({ ...ready, pairableDevices: [device] });
  render(
    <DeviceStatus
      bootstrap={{ ...bootstrap, device: { ...bootstrap.device, platform: 'kaihong' } }}
    />,
  );
  await waitFor(() => expect(screen.getByRole('button', { name: '搜索设备' })).toBeEnabled());
  expect(devicesApi.act).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '搜索设备' }));
  await screen.findByText(device.deviceName);
  expect(devicesApi.act).toHaveBeenCalledWith('discover', '');
  fireEvent.click(screen.getByRole('button', { name: '配对' }));
  const dialog = screen.getByRole('dialog', { name: '配对设备' });
  expect(devicesApi.act).toHaveBeenCalledTimes(1);
  fireEvent.click(within(dialog).getByRole('button', { name: '配对设备' }));
  await waitFor(() =>
    expect(devicesApi.act).toHaveBeenLastCalledWith('pair', device.deviceIdSha256),
  );
});

it('distinguishes paired devices from a ready peer and confirms removal', async () => {
  vi.mocked(devicesApi.status).mockResolvedValue({
    ...ready,
    trustedDevices: [{ ...device, online: true }],
  });
  render(
    <DeviceStatus
      bootstrap={{ ...bootstrap, device: { ...bootstrap.device, platform: 'kaihong' } }}
    />,
  );
  await screen.findByText('M-Claw 未就绪');
  expect(screen.queryByText('可协作')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '解除配对' }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }));
  expect(devicesApi.act).not.toHaveBeenCalled();
});

it('keeps the pending native operation visible after reopening the page', async () => {
  vi.mocked(devicesApi.status).mockResolvedValue({
    ...ready,
    operation: {
      id: 'op',
      action: 'pair',
      device_id: device.deviceIdSha256,
      status: 'running',
      error: null,
    },
  });
  render(
    <DeviceStatus
      bootstrap={{ ...bootstrap, device: { ...bootstrap.device, platform: 'kaihong' } }}
    />,
  );
  await screen.findByText('等待系统配对完成，请查看两台设备上的提示…');
  expect(screen.getByRole('button', { name: '搜索设备' })).toBeDisabled();
});

it('uses the backend capability status for Kaihong OS', () => {
  render(
    <DeviceStatus
      bootstrap={{ ...bootstrap, device: { ...bootstrap.device, platform: 'kaihong' } }}
    />,
  );
  expect(screen.getByText('当前设备支持 Kaihong OS 跨设备协同。')).toBeVisible();
});
