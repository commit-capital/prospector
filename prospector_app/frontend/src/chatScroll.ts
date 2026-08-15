export interface ChatScrollMetrics {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

export const CHAT_BOTTOM_THRESHOLD_PX = 24;

export function isChatAtBottom(metrics: ChatScrollMetrics): boolean {
  const distance = metrics.scrollHeight - metrics.clientHeight - metrics.scrollTop;
  return distance <= CHAT_BOTTOM_THRESHOLD_PX;
}

export function shouldFollowChatAfterScroll(
  previousScrollTop: number,
  metrics: ChatScrollMetrics,
): boolean {
  const movedUp = metrics.scrollTop < previousScrollTop;
  return !movedUp && isChatAtBottom(metrics);
}
