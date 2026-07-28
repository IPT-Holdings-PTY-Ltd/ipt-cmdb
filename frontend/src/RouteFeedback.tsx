import ErrorOutlineOutlined from '@mui/icons-material/ErrorOutlineOutlined';
import RefreshOutlined from '@mui/icons-material/RefreshOutlined';
import { Alert, Box, Button, CircularProgress, Stack, Typography } from '@mui/material';
import { Component, type ErrorInfo, type ReactNode } from 'react';

type RouteErrorBoundaryProps = {
  children: ReactNode;
};

type RouteErrorBoundaryState = {
  error: Error | null;
};

/** Identifies the messages browsers emit when an old deployment references a removed lazy chunk. */
export function isStaleChunkError(error: Error) {
  return /loading chunk|failed to fetch dynamically imported module|importing a module script failed|error loading dynamically imported module/i.test(error.message);
}

/** Consistent loading state for protected routes and lazy feature bundles. */
export function RouteLoadingFallback() {
  return (
    <Box sx={{ display: 'grid', minHeight: '45vh', placeItems: 'center' }}>
      <CircularProgress aria-label="Loading workspace" />
    </Box>
  );
}

/**
 * Keeps lazy-route and render failures inside the application shell and gives
 * stale browser sessions a safe recovery path after a deployment.
 */
export class RouteErrorBoundary extends Component<RouteErrorBoundaryProps, RouteErrorBoundaryState> {
  state: RouteErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RouteErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Route rendering failed', error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    const staleChunk = isStaleChunkError(this.state.error);
    return (
      <Box sx={{ minHeight: '45vh', display: 'grid', placeItems: 'center' }}>
        <Stack spacing={2} sx={{ width: '100%', maxWidth: 620 }}>
          <Alert severity="error" icon={<ErrorOutlineOutlined />} role="alert">
            <Typography variant="h6">
              {staleChunk ? 'A newer version is available' : 'This screen could not be displayed'}
            </Typography>
            <Typography variant="body2">
              {staleChunk
                ? 'Reload IPT CMDB to use the latest application files.'
                : 'Your session is still active. Reload the application, or use the navigation menu to open another screen.'}
            </Typography>
          </Alert>
          <Button
            variant="contained"
            startIcon={<RefreshOutlined />}
            onClick={() => window.location.reload()}
            sx={{ alignSelf: 'flex-start' }}
          >
            Reload application
          </Button>
        </Stack>
      </Box>
    );
  }
}
