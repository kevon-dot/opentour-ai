import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { CategoryChart } from '../../../components/foundation/data-display/category-chart';
import { categoryFixtures } from '../../../components/foundation/data-display/category-chart.fixtures';

export type CategoryFixtureName = keyof typeof categoryFixtures;
declare global { interface Window { showCategoryFixture(name: CategoryFixtureName): void } }

function Fixture({ name }: { name: CategoryFixtureName }) {
  const [expanded, setExpanded] = useState(false);
  return <main><CategoryChart {...categoryFixtures[name]} expanded={expanded} onExpandedChange={setExpanded} /></main>;
}
const root = createRoot(document.getElementById('root')!);
window.showCategoryFixture = name => root.render(<Fixture key={name} name={name} />);
