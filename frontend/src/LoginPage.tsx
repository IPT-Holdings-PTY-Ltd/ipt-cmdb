import { useEffect, useState, type FormEvent } from 'react';
import { Alert, Box, Button, Paper, Stack, TextField, Typography } from '@mui/material';
import { useLogin } from 'react-admin';
import { BrandLogo, useMspBranding } from './branding';
import { getAuthConfiguration, type AuthConfiguration } from './authProvider';

export function LoginPage() {
  const login = useLogin();
  const { brand } = useMspBranding();
  const [username, setUsername] = useState('admin@example.com');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [configuration, setConfiguration] = useState<AuthConfiguration | null>(null);

  useEffect(() => {
    getAuthConfiguration().then(setConfiguration).catch(reason => setError(reason instanceof Error ? reason.message : 'Unable to load sign-in configuration'));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try { await login({ username, password }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to sign in'); }
    finally { setBusy(false); }
  }

  return (
    <Box className="login-page">
      <Paper component="form" onSubmit={submit} className="login-card" elevation={18}>
        <BrandLogo size={58} borderRadius={14} />
        <Typography variant="overline" color="primary">MSP asset intelligence</Typography>
        <Typography variant="h3" component="h1">{brand.name}</Typography>
        <Typography sx={{
          color: "text.secondary"
        }}>{brand.welcomeMessage || 'Sign in to the workspace assigned to your account.'}</Typography>
        <Stack spacing={2} sx={{ mt: 4 }}>
          {error && <Alert severity="error">{error}</Alert>}
          {configuration?.external && <Button type="button" variant="contained" size="large" onClick={() => window.location.assign(configuration.externalLoginUrl)}>Sign in with Microsoft</Button>}
          {configuration?.localLoginEnabled && <>
            {configuration.external && <Typography
              variant="caption"
              sx={{
                color: "text.secondary",
                textAlign: "center"
              }}>Break-glass administrator</Typography>}
            <TextField label="Email address" value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" required />
            <TextField label="Password" type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" required />
            <Button type="submit" variant={configuration.external ? 'outlined' : 'contained'} size="large" disabled={busy}>{busy ? 'Signing in…' : 'Open CMDB'}</Button>
          </>}
        </Stack>
        {configuration?.mode === 'local' && <Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            mt: 3,
            display: 'block'
          }}>Local development account: admin@example.com / ChangeMe!</Typography>}
      </Paper>
    </Box>
  );
}
