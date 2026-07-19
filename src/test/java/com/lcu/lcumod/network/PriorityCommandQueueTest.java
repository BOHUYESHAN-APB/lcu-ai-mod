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

    private static WireServer.WireCommand command(String id, String command) {
        return new WireServer.WireCommand(id, command, new JsonObject());
    }
}
