import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { Select } from './Select';

function Example({ searchable = false, editable = false, disabled = false }) {
  const [value, setValue] = useState('alpha');
  return (
    <>
      <label>
        模型
        <Select
          label="模型"
          value={value}
          onChange={setValue}
          searchable={searchable}
          editable={editable}
          disabled={disabled}
        >
          <option value="alpha">Alpha</option>
          <option value="locked" disabled>
            不可用
          </option>
          <option value="beta">Beta</option>
          <option value="gamma">Gamma</option>
        </Select>
      </label>
      <button>下一项</button>
      <output aria-label="选中值">{value}</output>
    </>
  );
}

it('selects with the keyboard, skips unavailable options, and restores focus', async () => {
  render(<Example />);
  const control = screen.getByRole('combobox', { name: '模型' });
  control.focus();
  await userEvent.keyboard('{ArrowDown}{ArrowDown}{Enter}');
  expect(screen.getByLabelText('选中值')).toHaveTextContent('beta');
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(control).toHaveFocus();
});

it('dismisses outside and with Escape without changing the selection', async () => {
  const parentEscape = vi.fn();
  render(
    <div
      onKeyDown={(event) => {
        if (event.key === 'Escape') parentEscape();
      }}
    >
      <Example />
    </div>,
  );
  const control = screen.getByRole('combobox');
  await userEvent.click(control);
  await userEvent.keyboard('{ArrowDown}{Escape}');
  expect(parentEscape).not.toHaveBeenCalled();
  expect(control).toHaveFocus();
  expect(screen.getByLabelText('选中值')).toHaveTextContent('alpha');
  await userEvent.click(control);
  await userEvent.click(screen.getByRole('button', { name: '下一项' }));
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
});

it('filters a long menu and commits the matched option with Enter', async () => {
  render(<Example searchable />);
  await userEvent.click(screen.getByRole('combobox'));
  const search = screen.getByRole('searchbox', { name: '搜索模型' });
  expect(search).toHaveFocus();
  await userEvent.type(search, 'gAm');
  expect(screen.queryByRole('option', { name: 'Alpha' })).not.toBeInTheDocument();
  await userEvent.keyboard('{Enter}');
  expect(screen.getByLabelText('选中值')).toHaveTextContent('gamma');
  expect(screen.getByRole('combobox')).toHaveFocus();
});

it('leaves an empty search unchanged and tabs to the next form field', async () => {
  render(<Example searchable />);
  await userEvent.click(screen.getByRole('combobox'));
  await userEvent.type(screen.getByRole('searchbox'), 'missing');
  expect(screen.getByText('没有匹配项')).toBeVisible();
  await userEvent.keyboard('{Enter}');
  expect(screen.getByLabelText('选中值')).toHaveTextContent('alpha');
  await userEvent.tab();
  expect(screen.getByRole('button', { name: '下一项' })).toHaveFocus();
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
});

it('supports typeahead, Home and End, and never opens disabled controls', async () => {
  const view = render(<Example />);
  const control = screen.getByRole('combobox');
  control.focus();
  await userEvent.keyboard('b{Enter}');
  expect(screen.getByLabelText('选中值')).toHaveTextContent('beta');
  await userEvent.click(control);
  await userEvent.keyboard('{End}{Enter}');
  expect(screen.getByLabelText('选中值')).toHaveTextContent('gamma');
  await userEvent.click(control);
  await userEvent.keyboard('{Home}{Enter}');
  expect(screen.getByLabelText('选中值')).toHaveTextContent('alpha');
  view.rerender(<Example disabled />);
  await userEvent.click(control);
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
});

it('preserves custom entries and also accepts a suggested value', async () => {
  render(<Example editable />);
  const input = screen.getByRole('combobox');
  await userEvent.clear(input);
  await userEvent.type(input, 'custom-model');
  await userEvent.keyboard('{Enter}');
  expect(screen.getByLabelText('选中值')).toHaveTextContent('custom-model');
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '模型选项' }));
  await userEvent.click(screen.getByRole('option', { name: 'Beta' }));
  expect(input).toHaveValue('beta');
});

it('fits the popup above a control near the bottom-right edge', async () => {
  render(<Example />);
  const control = screen.getByRole('combobox');
  const bounds = vi.spyOn(control.parentElement!, 'getBoundingClientRect').mockReturnValue({
    x: 980,
    y: 690,
    top: 690,
    bottom: 730,
    left: 980,
    right: 1100,
    width: 120,
    height: 40,
    toJSON: () => ({}),
  });
  await userEvent.click(control);
  const popup = screen.getByRole('listbox').parentElement!;
  expect(popup.style.bottom).not.toBe('');
  expect(parseFloat(popup.style.left) + parseFloat(popup.style.width)).toBeLessThanOrEqual(
    window.innerWidth - 12,
  );
  fireEvent.scroll(window);
  expect(bounds).toHaveBeenCalled();
  bounds.mockRestore();
});

it('honors a disabled fieldset even when a popup was already open', async () => {
  const save = vi.fn();
  const form = (disabled: boolean) => (
    <fieldset disabled={disabled}>
      <Select label="区域" value="a" onChange={save}>
        <option value="a">A</option>
        <option value="b">B</option>
      </Select>
    </fieldset>
  );
  const view = render(form(false));
  await userEvent.click(screen.getByRole('combobox'));
  view.rerender(form(true));
  const option = screen.queryByRole('option', { name: 'B' });
  if (option) await userEvent.click(option);
  expect(save).not.toHaveBeenCalled();
});
