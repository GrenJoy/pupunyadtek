
import { ScheduleStatus } from '../types';

export const fileToBase64 = (file: File): Promise<string> => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = () => {
      const result = reader.result as string;
      resolve(result.split(',')[1]);
    };
    reader.onerror = (error) => reject(error);
  });
};

export const getScheduleStatus = (scheduleIntervals: string[] | undefined, currentTime: Date): ScheduleStatus => {
    if (!scheduleIntervals || scheduleIntervals.length === 0) {
        return {
            status: 'ON',
            message: '✅ Свет должен быть. График не загружен.',
            nextChange: '-',
            timeToNextChange: '',
        };
    }

    const today = new Date();
    today.setHours(0, 0, 0, 0);

    const intervals = scheduleIntervals.map(interval => {
        const [startStr, endStr] = interval.split('-').map(s => s.trim());
        const [startHours, startMinutes] = startStr.split(':').map(Number);
        const [endHours, endMinutes] = endStr.split(':').map(Number);

        const start = new Date(today);
        start.setHours(startHours, startMinutes, 0, 0);
        
        const end = new Date(today);
        end.setHours(endHours, endMinutes, 0, 0);

        return { start, end };
    }).sort((a, b) => a.start.getTime() - b.start.getTime());

    for (const interval of intervals) {
        if (currentTime >= interval.start && currentTime < interval.end) {
            const timeDiff = interval.end.getTime() - currentTime.getTime();
            return {
                status: 'OFF',
                message: '❌ Сейчас свет отключён.',
                nextChange: `✅ Ближайшее включение: ${interval.end.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`,
                timeToNextChange: `(через ${formatTimeDiff(timeDiff)})`
            };
        }
    }
    
    const nextOutage = intervals.find(interval => currentTime < interval.start);

    if (nextOutage) {
        const timeDiff = nextOutage.start.getTime() - currentTime.getTime();
        return {
            status: 'ON',
            message: '✅ Сейчас свет есть.',
            nextChange: `❌ Ближайшее отключение: ${nextOutage.start.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`,
            timeToNextChange: `(через ${formatTimeDiff(timeDiff)})`
        };
    }

    return {
        status: 'ON',
        message: '✅ Свет уже есть.',
        nextChange: 'Отключений на сегодня больше нет.',
        timeToNextChange: ''
    };
};


const formatTimeDiff = (ms: number): string => {
    const totalMinutes = Math.floor(ms / (1000 * 60));
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    
    let result = '';
    if (hours > 0) result += `${hours} ч `;
    if (minutes > 0) result += `${minutes} мин`;
    
    return result.trim();
};
