import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { NcentralPreviewProgressPanel } from './NcentralIntegration';
import type { NcentralPreviewRun } from './types';

function previewRun(
  changes: Partial<NcentralPreviewRun> = {},
): NcentralPreviewRun {
  return {
    id: 'run-1',
    companyId: 'acme',
    providerCompanyId: '101',
    policyId: 'policy-1',
    policyRevision: 2,
    status: 'running',
    phase: 'enriching_devices',
    progress: {
      current: 10,
      total: 25,
      percent: 40,
      discovered: 125,
      enriched: 10,
      reviewed: 0,
    },
    message: 'Enriching device evidence.',
    canCancel: true,
    canRetry: false,
    cancelRequested: false,
    ...changes,
  };
}

describe('N-central preview progress', () => {
  it('shows live progress and exposes cancellation without blocking the page', async () => {
    const cancel = vi.fn<() => void>();
    render(<NcentralPreviewProgressPanel
      run={previewRun()}
      busy={false}
      onCancel={cancel}
      onRetry={() => undefined}
    />);

    expect(screen.getByRole('progressbar', { name: 'N-central preview progress' }))
      .toHaveAttribute('aria-valuenow', '40');
    expect(screen.getByText('125 discovered')).toBeVisible();
    expect(screen.getByText('10 enriched')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
    expect(cancel).toHaveBeenCalledOnce();
  });

  it('stops cancellation and offers retry after a sanitized failure', async () => {
    const retry = vi.fn<() => void>();
    render(<NcentralPreviewProgressPanel
      run={previewRun({
        status: 'failed',
        phase: 'failed',
        message: 'Preview failed.',
        error: 'N-central returned a temporary provider error.',
        canCancel: false,
        canRetry: true,
      })}
      busy={false}
      onCancel={() => undefined}
      onRetry={retry}
    />);

    expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('temporary provider error');
    await userEvent.click(screen.getByRole('button', { name: 'Retry run' }));
    expect(retry).toHaveBeenCalledOnce();
  });
});
