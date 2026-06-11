package com.lcu.lcumod.network;

import com.google.gson.JsonObject;
import java.util.concurrent.PriorityBlockingQueue;

/**
 * Priority-based command queue for the wire protocol.
 * High-priority commands (flee, reflex) jump ahead of backend commands.
 */
public class PriorityCommandQueue {

    private final PriorityBlockingQueue<PriorityEntry> queue = new PriorityBlockingQueue<>(64,
            (a, b) -> Integer.compare(a.priority, b.priority));

    private volatile int currentPriority = CommandPriority.BACKEND;

    public void submitBackend(WireServer.WireCommand cmd) {
        PriorityEntry entry = new PriorityEntry(CommandPriority.BACKEND,
                cmd.id(), cmd.cmd(), cmd.args());
        queue.offer(entry);
        currentPriority = entry.priority;
    }

    public void submitBehavior(int priority, String id, String action, JsonObject args) {
        queue.offer(new PriorityEntry(priority, id, action, args));
        if (priority < currentPriority) {
            currentPriority = priority;
        }
    }

    public WireServer.WireCommand poll() {
        PriorityEntry entry = queue.poll();
        if (entry == null) return null;
        currentPriority = queue.isEmpty() ? CommandPriority.BACKEND : queue.peek().priority;
        return entry.cmd();
    }

    public WireServer.WireCommand take() throws InterruptedException {
        PriorityEntry entry = queue.take();
        currentPriority = queue.isEmpty() ? CommandPriority.BACKEND : queue.peek().priority;
        return entry.cmd();
    }

    public int getCurrentPriority() {
        return currentPriority;
    }

    public void clear() {
        queue.clear();
        currentPriority = CommandPriority.BACKEND;
    }

    public boolean isEmpty() {
        return queue.isEmpty();
    }

    public boolean hasEntries() { return !isEmpty(); }

    private record PriorityEntry(int priority, String id, String action, JsonObject args) {
        WireServer.WireCommand cmd() {
            return new WireServer.WireCommand(id, action, args);
        }
    }
}
