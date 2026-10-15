import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { LoaderCircle, Monitor, Search, X } from 'lucide-react';
import { devicesApi } from '../api';
import type { DeviceSnapshot } from '../types';
import { usePickerPosition } from './usePickerPosition';

export function DevicesPicker({
  selected,
  onChange,
  onNames,
  disabled = false,
}: {
  selected: string[];
  onChange: (ids: string[]) => void;
  onNames: (names: Record<string, string>) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<DeviceSnapshot | null>(null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const position = usePickerPosition(open, anchor);
  useEffect(() => {
    let active = true;
    let reading = false;
    const read = async () => {
      if (reading) return;
      reading = true;
      setLoading(true);
      try {
        const result = await devicesApi.status();
        if (active) {
          setSnapshot(result);
          onNames(
            Object.fromEntries([
              ...result.trustedDevices.map((device) => [device.publicDeviceId, device.deviceName]),
              ...result.devices.map((device) => [device.deviceId, device.deviceName]),
            ]),
          );
          setError('');
        }
      } catch (error) {
        if (active) setError(error instanceof Error ? error.message : '设备列表读取失败');
      } finally {
        reading = false;
        if (active) setLoading(false);
      }
    };
    void read();
    const timer = open ? setInterval(() => void read(), 5000) : undefined;
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [open, onNames]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (
        !anchor.current?.contains(event.target as Node) &&
        !panel.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.isComposing) {
        setOpen(false);
        anchor.current?.focus();
      }
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);
  const devices = snapshot?.devices ?? [];
  const paired = snapshot?.trustedDevices ?? [];
  return (
    <>
      <button
        ref={anchor}
        type="button"
        className="workspace-button"
        disabled={disabled}
        aria-expanded={open}
        data-selected={selected.length > 0}
        onClick={() => setOpen(!open)}
      >
        <Monitor size={15} />
        <span>设备{selected.length ? ` ${selected.length}` : ''}</span>
      </button>
      {open &&
        createPortal(
          <section
            ref={panel}
            className="composer-picker skills-picker-popover devices-picker-popover"
            style={position}
            role="dialog"
            aria-label="选择协作设备"
          >
            <header className="picker-heading">
              <div>
                <strong>协作设备</strong>
                <small>可多选</small>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭设备选择"
                onClick={() => {
                  setOpen(false);
                  anchor.current?.focus();
                }}
              >
                <X size={15} />
              </button>
            </header>
            <label className="picker-search">
              <Search size={14} />
              <input
                aria-label="查找协作设备"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索设备名称"
              />
            </label>
            <button
              type="button"
              className={`device-choice device-choice-auto${selected.length ? '' : ' selected'}`}
              disabled={disabled}
              onClick={() => onChange([])}
            >
              <span>
                <strong>自动选择</strong>
                <small>按任务需要使用已配对且可协作的设备</small>
              </span>
              <input
                type="checkbox"
                readOnly
                checked={!selected.length}
                tabIndex={-1}
                aria-hidden="true"
              />
            </button>
            <div className="device-choice-list">
              {error && <p role="alert">{error}</p>}
              {loading && !snapshot && (
                <p className="muted">
                  <LoaderCircle size={14} className="spin" />
                  正在读取设备…
                </p>
              )}
              {snapshot && !snapshot.enabled && (
                <p className="muted">请先在设备页面启用跨设备协同。</p>
              )}
              {snapshot?.enabled && !paired.length && (
                <div className="picker-empty">
                  <Monitor size={22} />
                  <strong>还没有已配对设备</strong>
                  <span>可前往设备页面搜索并配对</span>
                </div>
              )}
              {paired
                .filter((device) => device.deviceName.toLowerCase().includes(query.toLowerCase()))
                .map((device) => {
                  const peer = devices.find((item) => item.deviceId === device.publicDeviceId);
                  const ready =
                    snapshot?.enabled && device.online && peer?.agentAvailability === 'READY';
                  const checked = selected.includes(device.publicDeviceId);
                  return (
                    <label
                      className={`device-choice${ready || checked ? '' : ' unavailable'}`}
                      key={device.deviceIdSha256}
                    >
                      <span className="picker-item-icon">
                        <Monitor size={17} />
                      </span>
                      <span>
                        <strong>{device.deviceName}</strong>
                        <small>
                          {!device.online ? '离线' : ready ? '可协作' : 'M-Claw 尚未就绪'}
                        </small>
                      </span>
                      <input
                        type="checkbox"
                        aria-label={`选择 ${device.deviceName}`}
                        checked={checked}
                        disabled={disabled || (!checked && (!ready || selected.length >= 20))}
                        onChange={() =>
                          onChange(
                            checked
                              ? selected.filter((id) => id !== device.publicDeviceId)
                              : [...selected, device.publicDeviceId],
                          )
                        }
                      />
                    </label>
                  );
                })}
            </div>
            <footer className="picker-footer">
              <span>{selected.length ? `已选 ${selected.length} 台` : '按任务自动选择设备'}</span>
              {selected.length > 0 && (
                <button type="button" disabled={disabled} onClick={() => onChange([])}>
                  恢复自动
                </button>
              )}
            </footer>
          </section>,
          document.body,
        )}
    </>
  );
}
