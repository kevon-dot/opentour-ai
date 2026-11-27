import { createRoot } from 'react-dom/client';
import { OperationState, type OperationStateProps } from '../../../components/foundation/async-state/operation-state';

const display = new URLSearchParams(location.search).get('display');
const container = document.getElementById('root');
if (!display || !container) throw new Error('Missing generated operation display fixture.');
const props: OperationStateProps = JSON.parse(display);
createRoot(container).render(<OperationState {...props} />);
