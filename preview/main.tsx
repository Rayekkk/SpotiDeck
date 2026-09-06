import { createRoot } from 'react-dom/client';
import { Mockup } from './Mockup';
import { PanelPreview } from './PanelPreview';
import './preview.css';

createRoot(document.getElementById('root')!).render(new URLSearchParams(location.search).has('panel') ? <PanelPreview/> : <Mockup/>);
