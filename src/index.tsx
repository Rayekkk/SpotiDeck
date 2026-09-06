import { definePlugin, useQuickAccessVisible } from '@decky/api';
import { staticClasses } from '@decky/ui';
import { App } from './App';
import { client } from './client';
import { Icon } from './icons';
import { disposeUpdates } from './updates';
function Content() { const visible = useQuickAccessVisible(); return <App client={client} visible={visible}/>; }
export default definePlugin(() => ({
  name: 'SpotiDeck',
  titleView: <div className={staticClasses.Title}>SpotiDeck</div>,
  content: <Content/>, icon: <Icon name="spotify"/>,
  alwaysRender: true,
  onDismount() { disposeUpdates(client); },
}));
