
export interface City {
  id: string;
  name: string;
}

export interface Person {
  id: string;
  name: string;
  cityId: string;
  group: string;
}

export type ScheduleData = Record<string, string[]>;

export type Schedules = Record<string, ScheduleData>;

export interface ScheduleStatus {
  status: 'ON' | 'OFF';
  message: string;
  nextChange: string;
  timeToNextChange: string;
}
