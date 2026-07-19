package com.lcu.lcumod.network;

import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.PriorityBlockingQueue;

/**
 * Priority-based command queue for the wire protocol.
 * High-priority commands (flee, reflex) jump ahead of backend commands.
 */
public class PriorityCommandQueue {
    private static final int MAX_PENDING_COMMANDS = 40;

    private final PriorityBlockingQueue<PriorityEntry> queue = new PriorityBlockingQueue<>(64,
            (a, b) -> {
                int priority = Integer.compare(a.priority, b.priority);
                return priority != 0 ? priority : Long.compare(a.sequence, b.sequence);
            });
    private final AtomicLong sequence = new AtomicLong();

    private volatile int currentPriority = CommandPriority.BACKEND;

    public synchronized boolean submitBackend(WireServer.WireCommand cmd) {
        if (queue.size() >= MAX_PENDING_COMMANDS) return false;
        PriorityEntry entry = new PriorityEntry(CommandPriority.BACKEND, sequence.getAndIncrement(),
                cmd.id(), cmd.cmd(), cmd.args());
        queue.offer(entry);
        currentPriority = entry.priority;
        return true;
    }

    public synchronized List<WireServer.WireCommand> submitControl(WireServer.WireCommand cmd) {
        List<PriorityEntry> entries = drainEntries();
        List<WireServer.WireCommand> discarded = new ArrayList<>();
        List<PriorityEntry> retained = new ArrayList<>();
        boolean stopRetained = false;
        for (PriorityEntry entry : entries) {
            if (entry.priority == CommandPriority.CONTROL) {
                if ("stop_all".equals(entry.action)) {
                    if (stopRetained) {
                        discarded.add(entry.cmd());
                        continue;
                    }
                    stopRetained = true;
                }
                retained.add(entry);
            } else {
                discarded.add(entry.cmd());
            }
        }

        boolean incomingStop = "stop_all".equals(cmd.cmd());
        if (incomingStop && stopRetained) {
            for (int i = 0; i < retained.size(); i++) {
                if ("stop_all".equals(retained.get(i).action)) {
                    discarded.add(retained.remove(i).cmd());
                    break;
                }
            }
            retained.add(new PriorityEntry(
                CommandPriority.CONTROL, sequence.getAndIncrement(), cmd.id(), cmd.cmd(), cmd.args()));
        } else if (!incomingStop && retained.size() >= MAX_PENDING_COMMANDS) {
            discarded.add(cmd);
        } else {
            if (incomingStop && retained.size() >= MAX_PENDING_COMMANDS) {
                int removable = -1;
                for (int i = 0; i < retained.size(); i++) {
                    if (!"stop_all".equals(retained.get(i).action)) {
                        removable = i;
                        break;
                    }
                }
                if (removable >= 0) discarded.add(retained.remove(removable).cmd());
            }
            retained.add(new PriorityEntry(
                CommandPriority.CONTROL, sequence.getAndIncrement(), cmd.id(), cmd.cmd(), cmd.args()));
        }
        retained.forEach(queue::offer);
        currentPriority = queue.isEmpty() ? CommandPriority.BACKEND : queue.peek().priority;
        return discarded;
    }

    public synchronized List<WireServer.WireCommand> submitStop(WireServer.WireCommand cmd) {
        return submitControl(cmd);
    }

    public synchronized void submitBehavior(int priority, String id, String action, JsonObject args) {
        if (queue.size() >= MAX_PENDING_COMMANDS) return;
        queue.offer(new PriorityEntry(priority, sequence.getAndIncrement(), id, action, args));
        if (priority < currentPriority) {
            currentPriority = priority;
        }
    }

    public synchronized WireServer.WireCommand poll() {
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

    public synchronized void clear() {
        queue.clear();
        currentPriority = CommandPriority.BACKEND;
    }

    public synchronized List<WireServer.WireCommand> drain() {
        return drainEntries().stream().map(PriorityEntry::cmd).toList();
    }

    private List<PriorityEntry> drainEntries() {
        List<PriorityEntry> entries = new ArrayList<>();
        queue.drainTo(entries);
        entries.sort((a, b) -> {
            int priority = Integer.compare(a.priority, b.priority);
            return priority != 0 ? priority : Long.compare(a.sequence, b.sequence);
        });
        currentPriority = CommandPriority.BACKEND;
        return entries;
    }

    public boolean isEmpty() {
        return queue.isEmpty();
    }

    public boolean hasEntries() { return !isEmpty(); }

    private record PriorityEntry(int priority, long sequence, String id, String action, JsonObject args) {
        WireServer.WireCommand cmd() {
            return new WireServer.WireCommand(id, action, args);
        }
    }
}
