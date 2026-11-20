import { createRoot } from 'react-dom/client';
import { ActivityHistory } from '../../../features/autonomy/activity-history';
import { activityFixture, type ActivityFixture } from '../../../features/autonomy/activity-history.fixtures';

declare global { interface Window { showActivityHistory(state: ActivityFixture): void; olderActivityCalls: number } }
const root = createRoot(document.getElementById('root')!);
window.olderActivityCalls = 0;
window.showActivityHistory = state => root.render(<main><ActivityHistory snapshot={activityFixture(state)}
  onLoadOlder={() => { window.olderActivityCalls += 1; }} /></main>);
