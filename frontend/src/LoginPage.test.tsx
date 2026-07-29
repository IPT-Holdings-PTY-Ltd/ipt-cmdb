import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as authProvider from './authProvider';
import { ApiError } from './session';

vi.mock('./branding', () => ({
  BrandLogo: () => <span aria-hidden="true">Logo</span>,
  useMspBranding: () => ({
    brand: {
      name: 'CMDB Hub',
      welcomeMessage: '',
    },
  }),
}));

import { LoginPage } from './LoginPage';

describe('local sign-in', () => {
  beforeEach(() => {
    vi.spyOn(authProvider, 'getAuthConfiguration').mockResolvedValue({
      mode: 'local',
      external: false,
      localLoginEnabled: true,
      passwordResetAvailable: false,
      externalLoginUrl: '',
      externalLogoutUrl: '',
      mfaAvailable: true,
      localMfaPolicy: 'all',
    });
  });

  it('starts blank and disables submission for the server Retry-After period', async () => {
    vi.spyOn(authProvider, 'login').mockRejectedValue(
      new ApiError(
        'Too many sign-in attempts. Try again later.',
        429,
        {},
        73,
      ),
    );
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    const email = await screen.findByRole('textbox', { name: 'Email address' });
    expect(email).toHaveValue('');
    expect(screen.queryByText(/ChangeMe!/)).not.toBeInTheDocument();
    await user.type(email, 'tech@example.com');
    await user.type(screen.getByLabelText(/Password/), 'not-the-password');
    await user.click(screen.getByRole('button', { name: 'Open CMDB' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Try again in 73s' })).toBeDisabled();
    });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Too many sign-in attempts. Try again later.',
    );
  });
});
