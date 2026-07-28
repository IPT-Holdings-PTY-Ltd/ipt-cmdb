import { Alert, Snackbar } from '@mui/material';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router';

type NotificationKind = 'success' | 'error' | 'info' | 'warning';
type Notification = { id: number; message: string; kind: NotificationKind };
type NotifyOptions = { type?: NotificationKind };
type Notify = (message: string, options?: NotifyOptions) => void;

const NotificationContext = createContext<Notify | null>(null);

/**
 * Provides lightweight global notifications without coupling the application to
 * an administration framework.
 */
export function NotificationProvider({ children }: { children: ReactNode }) {
  const [notification, setNotification] = useState<Notification | null>(null);
  const notify = useCallback<Notify>((message, options = {}) => {
    setNotification({ id: Date.now(), message, kind: options.type || 'info' });
  }, []);
  const value = useMemo(() => notify, [notify]);

  return (
    <NotificationContext.Provider value={value}>
      {children}
      <Snackbar
        key={notification?.id}
        open={Boolean(notification)}
        autoHideDuration={5000}
        onClose={() => setNotification(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      >
        {notification
          ? <Alert severity={notification.kind} variant="filled" onClose={() => setNotification(null)}>{notification.message}</Alert>
          : undefined}
      </Snackbar>
    </NotificationContext.Provider>
  );
}

/** Returns the global notification dispatcher. */
export function useNotify() {
  const notify = useContext(NotificationContext);
  if (!notify) throw new Error('NotificationProvider is missing');
  return notify;
}

/** Updates the browser title for the active workspace page. */
export function Title({ title }: { title: string }) {
  useEffect(() => {
    document.title = `${title} · IPT CMDB`;
  }, [title]);
  return null;
}

/** Compatibility helper for screens that navigate after an action. */
export function useRedirect() {
  const navigate = useNavigate();
  return useCallback((target: string) => navigate(target), [navigate]);
}
