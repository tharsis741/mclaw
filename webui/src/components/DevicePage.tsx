import { useEffect, useRef, useState } from 'react';
import {
  Check,
  LoaderCircle,
  Monitor,
  Network,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from 'lucide-react';
import { devicesApi } from '../api';
import type { Bootstrap, DeviceSnapshot, NearbyDevice } from '../types';

type Confirmation = { action: 'pair' | 'unpair'; device: NearbyDevice } | { action: 'disable' };

function DeviceConfirmation({
  confirmation,
  onClose,
  onConfirm,
}: {
  confirmation: Confirmation;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const pairing = confirmation.action === 'pair';
  const title = pairing
    ? '配对设备'
    : confirmation.action === 'unpair'
      ? '解除配对'
      : '停用设备协同';
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => previous?.focus();
  }, []);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal device-confirmation"
        ref={dialog}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="device-confirm-title"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === 'Escape') onClose();
          if (event.key === 'Tab') {
            const buttons = Array.from(
              dialog.current?.querySelectorAll<HTMLButtonElement>('button') ?? [],
            );
            const first = buttons[0];
            const last = buttons[buttons.length - 1];
            if (
              event.shiftKey &&
              (document.activeElement === first || document.activeElement === dialog.current)
            ) {
              event.preventDefault();
              last?.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <header>
          <h3 id="device-confirm-title">{title}</h3>
          <button className="icon-button" aria-label="关闭" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        <p>
          {confirmation.action === 'disable' ? (
            '停用后将断开设备连接。已建立的配对关系会保留。'
          ) : pairing ? (
            <>
              与 <strong>{confirmation.device.deviceName}</strong>{' '}
              建立信任关系。接下来请按系统提示，在两台设备上完成确认。
            </>
          ) : (
            <>
              与 <strong>{confirmation.device.deviceName}</strong>{' '}
              解除配对。再次协作时，需要重新配对。
            </>
          )}
        </p>
        <div className="dialog-actions">
          <button onClick={onClose}>取消</button>
          <button className="primary" onClick={onConfirm}>
            {title}
          </button>
        </div>
      </div>
    </div>
  );
}

export function DeviceStatus({ bootstrap }: { bootstrap: Bootstrap }) {
  const kaihong = bootstrap.device.platform.toLowerCase() === 'kaihong';
  const [snapshot, setSnapshot] = useState<DeviceSnapshot | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    if (!kaihong)
      return () => {
        mounted.current = false;
      };
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await devicesApi.status();
        if (mounted.current) {
          setSnapshot(next);
          setError('');
        }
      } catch (failure) {
        if (mounted.current)
          setError(failure instanceof Error ? failure.message : '无法读取设备状态');
      } finally {
        if (mounted.current) timer = setTimeout(() => void poll(), 3000);
      }
    };
    void poll();
    return () => {
      mounted.current = false;
      clearTimeout(timer);
    };
  }, [kaihong]);
  const operation = snapshot?.operation;
  const pending = busy || snapshot?.state === 'starting' || operation?.status === 'running';
  const usable = snapshot && ['ready', 'degraded'].includes(snapshot.state);
  const stateLabel = !kaihong
    ? '不可用'
    : !snapshot
      ? '正在读取'
      : ((
          {
            ready: '已启用',
            degraded: '连接异常',
            disabled: '未启用',
            starting: '正在连接',
            error: '连接失败',
            unsupported: '不可用',
          } as Record<string, string>
        )[snapshot.state] ?? '未连接');
  const perform = async (
    action: 'refresh' | 'enable' | 'disable' | 'discover' | 'pair' | 'unpair',
    deviceId = '',
  ) => {
    setBusy(true);
    setError('');
    setConfirmation(null);
    try {
      const next =
        action === 'refresh'
          ? await devicesApi.status()
          : action === 'enable' || action === 'disable'
            ? await devicesApi.configure(action === 'enable')
            : await devicesApi.act(action, deviceId);
      if (mounted.current) setSnapshot(next);
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : '设备操作失败');
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const deviceState = (device: DeviceSnapshot['trustedDevices'][number]) => {
    if (!device.online) return '离线';
    const peer = snapshot?.devices.find((item) => item.deviceId === device.publicDeviceId);
    if (peer?.agentAvailability === 'READY') return '可协作';
    if (peer && ['CONNECTING', 'RECONNECTING'].includes(peer.connectionState)) return '正在连接';
    return 'M-Claw 未就绪';
  };
  const deviceRows = (devices: NearbyDevice[], paired: boolean) =>
    devices.map((device) => (
      <div className="device-list-row" key={device.deviceIdSha256}>
        <span className="tool-icon">
          {paired ? <ShieldCheck size={20} /> : <Monitor size={20} />}
        </span>
        <div className="device-list-name">
          <strong>{device.deviceName}</strong>
          <small>
            {paired
              ? deviceState(device as DeviceSnapshot['trustedDevices'][number])
              : `设备编号 ${device.deviceIdSha256.slice(0, 8).toUpperCase()}`}
          </small>
        </div>
        <button
          disabled={!!pending || !usable}
          onClick={() => setConfirmation({ action: paired ? 'unpair' : 'pair', device })}
        >
          {paired ? '解除配对' : '配对'}
        </button>
      </div>
    ));
  const visibleError =
    error ||
    snapshot?.error?.message ||
    (operation?.status === 'failed' ? operation.error?.message : '');
  return (
    <section className="management-page device-page">
      <div className="page-heading">
        <h2>设备</h2>
        {kaihong && (
          <button
            className="icon-button"
            aria-label="刷新设备状态"
            disabled={!!pending}
            onClick={() => void perform('refresh')}
          >
            <RefreshCw size={17} />
          </button>
        )}
      </div>
      <section className="settings-section">
        <h3>跨设备协同</h3>
        <div className="device-collaboration">
          <span className="tool-icon">
            <Network size={22} />
          </span>
          <div>
            <strong>M-Claw 协同</strong>
            <p>
              {kaihong
                ? snapshot && !snapshot.supported && snapshot.state !== 'starting'
                  ? '当前系统尚未提供设备协同能力。'
                  : '当前设备支持 Kaihong OS 跨设备协同。'
                : '跨设备 M-Claw 协同不可用，仅支持 Kaihong OS 端。'}
            </p>
          </div>
          <span className={`status-label ${snapshot?.state === 'ready' ? 'good' : ''}`}>
            {stateLabel}
          </span>
          {snapshot?.supported && (
            <button
              disabled={!!pending}
              onClick={() =>
                snapshot.enabled && usable
                  ? setConfirmation({ action: 'disable' })
                  : void perform('enable')
              }
            >
              {snapshot.enabled && usable ? '停用' : snapshot.enabled ? '重试连接' : '启用'}
            </button>
          )}
        </div>
        {visibleError && (
          <p className="device-feedback error" role="alert">
            {visibleError}
          </p>
        )}
        {operation?.status === 'running' && (
          <p className="device-feedback" role="status">
            <LoaderCircle className="spin" size={14} />
            {operation.action === 'discover'
              ? '正在搜索附近的设备…'
              : operation.action === 'pair'
                ? '等待系统配对完成，请查看两台设备上的提示…'
                : '正在解除配对…'}
          </p>
        )}
        {operation?.status === 'completed' && operation.action !== 'discover' && (
          <p className="device-feedback" role="status">
            <Check size={14} />
            {operation.action === 'pair' ? '配对成功' : '已解除配对'}
          </p>
        )}
      </section>
      {kaihong && (
        <>
          <section className="settings-section">
            <div className="device-section-heading">
              <h3>已配对设备</h3>
              <span>{snapshot?.trustedDevices.length ?? 0}</span>
            </div>
            <div className="device-list">
              {snapshot?.trustedDevices.length ? (
                deviceRows(snapshot.trustedDevices, true)
              ) : (
                <p className="device-empty">
                  {usable ? '还没有已配对的设备。' : '启用协同后查看已配对设备。'}
                </p>
              )}
            </div>
          </section>
          <section className="settings-section">
            <div className="device-section-heading">
              <h3>附近设备</h3>
              <button disabled={!!pending || !usable} onClick={() => void perform('discover')}>
                <Search size={15} />
                搜索设备
              </button>
            </div>
            <div className="device-list">
              {snapshot?.pairableDevices.length ? (
                deviceRows(snapshot.pairableDevices, false)
              ) : (
                <p className="device-empty">
                  {operation?.action === 'discover' && operation.status === 'completed'
                    ? '没有找到可配对设备，请确认另一台设备已开启设备协同，并检查网络连接。'
                    : '搜索附近可配对的设备。'}
                </p>
              )}
            </div>
          </section>
        </>
      )}
      <section className="settings-section">
        <h3>当前设备</h3>
        <div className="setting-rows">
          <div className="setting-row">
            <strong>设备名称</strong>
            <span className="device-value">
              <Monitor size={16} />
              {snapshot?.localDevice?.deviceName || bootstrap.device.name}
            </span>
          </div>
          <div className="setting-row">
            <strong>操作系统</strong>
            <span>
              {snapshot?.localDevice
                ? `${snapshot.localDevice.osName} ${snapshot.localDevice.osVersion}`
                : kaihong
                  ? 'Kaihong OS'
                  : bootstrap.device.platform.toLowerCase() === 'windows'
                    ? 'Windows'
                    : bootstrap.device.platform}
            </span>
          </div>
          {snapshot?.localDevice && (
            <div className="setting-row">
              <strong>设备型号</strong>
              <span>{snapshot.localDevice.model}</span>
            </div>
          )}
          <div className="setting-row">
            <strong>M-Claw 版本</strong>
            <span>{bootstrap.version}</span>
          </div>
        </div>
      </section>
      {confirmation && (
        <DeviceConfirmation
          confirmation={confirmation}
          onClose={() => setConfirmation(null)}
          onConfirm={() =>
            void perform(
              confirmation.action,
              'device' in confirmation ? confirmation.device.deviceIdSha256 : '',
            )
          }
        />
      )}
    </section>
  );
}
