const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('InfiniteCanvasDesktop', {
  isDesktopClient: true,
  checkClientUpdate: () => ipcRenderer.invoke('client-update:check'),
  onClientUpdateAvailable: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('client-update:available', listener);
    return () => ipcRenderer.removeListener('client-update:available', listener);
  },
  respondToClientUpdate: (requestId, action, source) =>
    ipcRenderer.invoke('client-update:respond', { requestId, action, source }),
  probeClientUpdateConnectivity: (requestId, targetId) =>
    ipcRenderer.invoke('client-update:probe-connectivity', { requestId, targetId }),
});
