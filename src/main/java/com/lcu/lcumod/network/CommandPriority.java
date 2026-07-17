package com.lcu.lcumod.network;

/**
 * Priority levels for commands and behaviors.
 * Higher priority commands can cancel lower priority ones.
 */
public final class CommandPriority {
    /** Control ownership transition — clears all queued work. */
    public static final int CONTROL = -1;
    /** Flee from danger — highest, cannot be interrupted */
    public static final int FLEE = 0;
    /** Combat reflex — attacked, immediate counter-attack */
    public static final int REFLEX = 1;
    /** Auto-eat, auto-torch, real-time survival */
    public static final int SURVIVAL = 2;
    /** Item pickup — walk to nearby items */
    public static final int PICKUP = 3;
    /** Backend command (move_to, mine_block, etc.) */
    public static final int BACKEND = 4;
    /** Autonomous behavior (exploring, idle) */
    public static final int AUTONOMOUS = 5;

    private CommandPriority() {}
}
