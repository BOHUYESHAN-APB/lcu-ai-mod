package com.lcu.lcumod.action;

public enum BodyPriority {
    IDLE(10),
    AUTONOMY(30),
    BACKEND_DIRECT(60),
    FOREGROUND(80),
    SAFETY(100),
    MANUAL(120);

    private final int value;

    BodyPriority(int value) {
        this.value = value;
    }

    int value() {
        return value;
    }
}
