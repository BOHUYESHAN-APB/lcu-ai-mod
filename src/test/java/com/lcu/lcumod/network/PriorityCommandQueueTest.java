package com.lcu.lcumod.network;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;

class PriorityCommandQueueTest {
    @Test
    void backendCommandsWithEqualPriorityRemainFifo() {
        PriorityCommandQueue queue = new PriorityCommandQueue();
        queue.submitBackend(command("first", "jump"));
        queue.submitBackend(command("second", "eat"));

        assertEquals("first", queue.poll().id());
        assertEquals("second", queue.poll().id());
    }

    @Test
    void stopAtomicallyDiscardsQueuedWorkAndRunsAtControlPriority() {
        PriorityCommandQueue queue = new PriorityCommandQueue();
        queue.submitBackend(command("move", "move_to"));
        queue.submitBackend(command("craft", "craft_item"));

        var discarded = queue.submitStop(command("stop", "stop_all"));

        assertEquals(java.util.List.of("move", "craft"), discarded.stream().map(WireServer.WireCommand::id).toList());
        assertEquals(CommandPriority.CONTROL, queue.getCurrentPriority());
        assertEquals("stop", queue.poll().id());
    }

    @Test
    void disarmAtomicallyDiscardsQueuedWorkAndRunsAtControlPriority() {
        PriorityCommandQueue queue = new PriorityCommandQueue();
        queue.submitBackend(command("move", "move_to"));
        queue.submitBackend(command("craft", "craft_item"));

        var discarded = queue.submitStop(command("disarm", "disarm"));

        assertEquals(java.util.List.of("move", "craft"),
                discarded.stream().map(WireServer.WireCommand::id).toList());
        assertEquals("disarm", queue.poll().id());
    }

    @Test
    void laterStopCannotDowngradeQueuedDisarm() {
        PriorityCommandQueue queue = new PriorityCommandQueue();
        queue.submitStop(command("disarm", "disarm"));

        var discarded = queue.submitStop(command("stop", "stop_all"));

        assertEquals(java.util.List.of("stop"),
                discarded.stream().map(WireServer.WireCommand::id).toList());
        assertEquals("disarm", queue.poll().id());
    }

    @Test
    void laterControlTransitionDoesNotDiscardQueuedStop() {
        PriorityCommandQueue queue = new PriorityCommandQueue();
        queue.submitStop(command("stop", "stop_all"));
        queue.submitBackend(command("work", "craft_item"));

        var discarded = queue.submitControl(command("control", "control_external"));

        assertEquals(java.util.List.of("work"), discarded.stream().map(WireServer.WireCommand::id).toList());
        assertEquals("stop", queue.poll().id());
        assertEquals("control", queue.poll().id());
    }

    @Test
    void stopDoesNotDiscardEarlierControlTransition() {
        PriorityCommandQueue queue = new PriorityCommandQueue();
        queue.submitControl(command("control", "control_builtin"));
        queue.submitBackend(command("work", "craft_item"));

        var discarded = queue.submitStop(command("stop", "stop_all"));

        assertEquals(java.util.List.of("work"), discarded.stream().map(WireServer.WireCommand::id).toList());
        assertEquals("control", queue.poll().id());
        assertEquals("stop", queue.poll().id());
    }

    @Test
    void backendQueueIsBoundedButControlStopStillPreemptsIt() {
        PriorityCommandQueue queue = new PriorityCommandQueue();
        for (int i = 0; i < 40; i++) {
            assertEquals(true, queue.submitBackend(command("work-" + i, "jump")));
        }

        assertEquals(false, queue.submitBackend(command("overflow", "jump")));
        var discarded = queue.submitStop(command("stop", "stop_all"));
        assertEquals(40, discarded.size());
        assertEquals("stop", queue.poll().id());
    }

    @Test
    void duplicateStopsAreCoalescedAndControlQueueRemainsBounded() {
        PriorityCommandQueue queue = new PriorityCommandQueue();
        queue.submitStop(command("stop-1", "stop_all"));

        var duplicate = queue.submitStop(command("stop-2", "stop_all"));
        for (int i = 0; i < 39; i++) {
            queue.submitControl(command("control-" + i, "control_builtin"));
        }
        var overflow = queue.submitControl(command("overflow", "control_external"));

        assertEquals(java.util.List.of("stop-1"), duplicate.stream().map(WireServer.WireCommand::id).toList());
        assertEquals(java.util.List.of("overflow"), overflow.stream().map(WireServer.WireCommand::id).toList());
        assertEquals("stop-2", queue.poll().id());
    }

    private static WireServer.WireCommand command(String id, String command) {
        return new WireServer.WireCommand(id, command, new JsonObject());
    }
}
