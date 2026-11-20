import { createRoot } from 'react-dom/client';
import { ArtifactManifest } from '../../../features/artifacts/artifact-manifest';
import { manifestFixture, type ManifestFixture } from '../../../features/artifacts/artifact-manifest.fixtures';

declare global { interface Window { showArtifactManifest(state: ManifestFixture): void; manifestWrites: string[] } }
const root = createRoot(document.getElementById('root')!);
window.manifestWrites = [];
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
  writeText: async (value: string) => { window.manifestWrites.push(value); },
} });
window.showArtifactManifest = state => root.render(<main><ArtifactManifest {...manifestFixture(state)} /></main>);
