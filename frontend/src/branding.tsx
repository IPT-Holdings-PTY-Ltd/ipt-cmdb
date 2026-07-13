import { Box } from '@mui/material';
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { apiFetch } from './session';

export type Brand = {
  name: string;
  logoText: string;
  accent: string;
  secondaryAccent: string;
  logoDataUrl: string;
  logoFileName: string;
  supportEmail: string;
  supportUrl: string;
  supportPhone: string;
  welcomeMessage: string;
  reportFooter: string;
  confidentialityLabel: string;
};

export const DEFAULT_MSP_BRAND: Brand = {
  name: 'CMDB Hub', logoText: 'C', accent: '#50d5b9', secondaryAccent: '#7997ff',
  logoDataUrl: '', logoFileName: '', supportEmail: '', supportUrl: '', supportPhone: '',
  welcomeMessage: '', reportFooter: '', confidentialityLabel: 'Internal use only',
};

type BrandingState = { brand: Brand; refresh: () => Promise<Brand> };
const BrandingContext = createContext<BrandingState | null>(null);

export function BrandingProvider({ children }: { children: ReactNode }) {
  const [brand, setBrand] = useState<Brand>(DEFAULT_MSP_BRAND);
  const refresh = async () => {
    const value = await apiFetch<Partial<Brand>>('/api/branding/public');
    const merged = { ...DEFAULT_MSP_BRAND, ...value };
    setBrand(merged);
    return merged;
  };

  useEffect(() => {
    void refresh().catch(() => setBrand(DEFAULT_MSP_BRAND));
    const changed = () => { void refresh(); };
    window.addEventListener('cmdb-branding-change', changed);
    return () => window.removeEventListener('cmdb-branding-change', changed);
  }, []);

  const value = useMemo(() => ({ brand, refresh }), [brand]);
  return <BrandingContext.Provider value={value}>{children}</BrandingContext.Provider>;
}

export function useMspBranding() {
  const value = useContext(BrandingContext);
  if (!value) throw new Error('BrandingProvider is missing');
  return value;
}

export function BrandLogo({ size = 44, borderRadius = 10 }: { size?: number; borderRadius?: number }) {
  const { brand } = useMspBranding();
  if (brand.logoDataUrl) {
    return <Box component="img" src={brand.logoDataUrl} alt={`${brand.name} logo`} sx={{ width: size, height: size, objectFit: 'contain', borderRadius, flex: 'none' }} />;
  }
  return <Box aria-label={`${brand.name} logo fallback`} sx={{ width: size, height: size, borderRadius: `${borderRadius}px`, flex: 'none', display: 'grid', placeItems: 'center', color: '#07131d', background: `linear-gradient(135deg, ${brand.accent}, ${brand.secondaryAccent})`, fontWeight: 900, letterSpacing: '-0.06em' }}>{brand.logoText || 'C'}</Box>;
}
