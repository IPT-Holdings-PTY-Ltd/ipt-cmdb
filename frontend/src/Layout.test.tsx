import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { NavigationLink } from './Layout';

describe('workspace navigation links', () => {
  it('uses native link semantics and closes the mobile drawer', async () => {
    const close = vi.fn<() => void>();
    render(
      <MemoryRouter initialEntries={['/assets']}>
        <NavigationLink
          close={close}
          item={{ path: '/assets', label: 'Assets', icon: <span aria-hidden="true">A</span> }}
        />
      </MemoryRouter>,
    );

    const link = screen.getByRole('link', { name: 'Assets' });
    expect(link).toHaveAttribute('href', '/assets');
    expect(link).toHaveClass('Mui-selected');

    await userEvent.click(link);
    expect(close).toHaveBeenCalledOnce();
  });
});
