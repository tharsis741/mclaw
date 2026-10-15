import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { devicesApi } from '../api';
import type { DeviceSnapshot } from '../types';
import { DevicesPicker } from './DevicesPicker';

vi.mock('../api', () => ({ devicesApi: { status: vi.fn(), act: vi.fn() } }));
const ids = ['a', 'b', 'c'].map((letter) => `urn:mclaw:device:oh:${letter.repeat(64)}`);
const snapshot: DeviceSnapshot = {
  supported: true,
  enabled: true,
  state: 'ready',
  provider_ready: true,
  error: null,
  operation: null,
  localDevice: null,
  pairableDevices: [],
  discoveryWarning: null,
  trustedDevices: ids.map((id, index) => ({
    publicDeviceId: id,
    deviceIdSha256: id.slice(-64),
    deviceName: `电脑 ${index + 1}`,
    deviceTypeId: 12,
    online: index !== 1,
  })),
  devices: [
    {
      deviceId: ids[0],
      deviceName: '电脑 1',
      agentAvailability: 'READY',
      connectionState: 'CONNECTED',
      devicePresence: 'ONLINE',
    },
  ],
};
const names = vi.fn();
function Picker({ disabled = false }: { disabled?: boolean }) {
  const [selected, setSelected] = useState([ids[1]]);
  return (
    <DevicesPicker selected={selected} onChange={setSelected} onNames={names} disabled={disabled} />
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(devicesApi.status).mockResolvedValue(snapshot);
});

it('selects only ready paired devices, allows removal of an offline selection, and restores auto mode', async () => {
  render(<Picker />);
  await userEvent.click(screen.getByRole('button', { name: '设备 1' }));
  const ready = await screen.findByRole('checkbox', { name: '选择 电脑 1' });
  const offline = screen.getByRole('checkbox', { name: '选择 电脑 2' });
  expect(offline).toBeChecked();
  expect(screen.getByRole('checkbox', { name: '选择 电脑 3' })).toBeDisabled();
  await userEvent.click(offline);
  expect(offline).toBeDisabled();
  await userEvent.click(ready);
  expect(ready).toBeChecked();
  await userEvent.click(screen.getByRole('button', { name: /自动选择/ }));
  expect(ready).not.toBeChecked();
  expect(devicesApi.act).not.toHaveBeenCalled();
});

it('keeps search and selections on outside close and restores trigger focus on Escape', async () => {
  render(
    <>
      <Picker />
      <button>其他区域</button>
    </>,
  );
  await userEvent.click(screen.getByRole('button', { name: '设备 1' }));
  await userEvent.type(screen.getByRole('textbox', { name: '查找协作设备' }), '电脑 1');
  await userEvent.click(await screen.findByRole('checkbox', { name: '选择 电脑 1' }));
  expect(screen.getByRole('dialog')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '其他区域' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  const trigger = screen.getByRole('button', { name: '设备 2' });
  await userEvent.click(trigger);
  expect(screen.getByRole('checkbox', { name: '选择 电脑 1' })).toBeChecked();
  await userEvent.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(trigger).toHaveFocus();
});

it('shows an empty paired list clearly and locks all selection controls when the session is occupied', async () => {
  vi.mocked(devicesApi.status).mockResolvedValue({ ...snapshot, trustedDevices: [], devices: [] });
  const view = render(<Picker />);
  await userEvent.click(screen.getByRole('button', { name: '设备 1' }));
  expect(await screen.findByText(/还没有已配对设备/)).toBeVisible();
  view.rerender(<Picker disabled />);
  expect(screen.getByRole('button', { name: /自动选择/ })).toBeDisabled();
});
