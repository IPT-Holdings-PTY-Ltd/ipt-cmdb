import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { RouteErrorBoundary, RouteLoadingFallback, isStaleChunkError } from './RouteFeedback';

function BrokenRoute(): never {
  throw new Error('Failed to fetch dynamically imported module');
}

describe('route feedback', () => {
  it('recognizes stale deployment chunk failures', () => {
    expect(isStaleChunkError(new Error('Loading chunk 42 failed'))).toBe(true);
    expect(isStaleChunkError(new Error('Ordinary render error'))).toBe(false);
  });

  it('offers a reload when a lazy route cannot be loaded', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(<RouteErrorBoundary><BrokenRoute /></RouteErrorBoundary>);

    expect(screen.getByRole('alert')).toHaveTextContent('A newer version is available');
    expect(screen.getByRole('button', { name: 'Reload application' })).toBeVisible();
  });

  it('exposes an accessible lazy loading state', () => {
    render(<RouteLoadingFallback />);
    expect(screen.getByLabelText('Loading workspace')).toBeVisible();
  });
});
