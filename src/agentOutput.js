export function agentExplanation(output) {
  let latest = '';
  for (const line of (output || '').split('\n')) {
    try {
      const event = JSON.parse(line);
      if (event.type === 'item.completed' && event.item?.type === 'agent_message' && typeof event.item.text === 'string') latest = event.item.text;
    } catch { /* Partial stream lines and plain process output stay in raw output. */ }
  }
  return latest;
}
