
import React, { useState, useEffect, useMemo } from 'react';
import { City, Person, Schedules, ScheduleData, ScheduleStatus } from './types';
import { ELECTRICITY_GROUPS } from './constants';
import { getScheduleStatus } from './utils/helpers';
import { UserIcon, CalendarIcon, CityIcon, UploadIcon, PlusIcon, BackIcon, TrashIcon, EditIcon, LightningIcon } from './components/icons';

type View = 'main' | 'manage_people' | 'add_person' | 'edit_person' | 'manage_cities' | 'upload_schedule' | 'view_schedule';

// Helper component defined outside App to prevent re-renders
const MainButton: React.FC<{ icon: React.ReactNode; label: string; onClick: () => void }> = ({ icon, label, onClick }) => (
    <button onClick={onClick} className="flex flex-col items-center justify-center space-y-2 bg-secondary p-6 rounded-lg shadow-lg hover:bg-accent transition-all duration-300 transform hover:-translate-y-1 w-full animate-slide-in-up">
        <div className="text-action">{icon}</div>
        <span className="text-text-primary font-semibold text-center">{label}</span>
    </button>
);

const App: React.FC = () => {
    const [view, setView] = useState<View>('main');
    const [cities, setCities] = useState<City[]>([]);
    const [people, setPeople] = useState<Person[]>([]);
    const [schedules, setSchedules] = useState<Schedules>({});

    const [selectedCityId, setSelectedCityId] = useState<string | null>(null);
    const [selectedPersonId, setSelectedPersonId] = useState<string | null>(null);
    const [editingPerson, setEditingPerson] = useState<Person | null>(null);

    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [currentTime, setCurrentTime] = useState(new Date());

    // --- API Calls ---

    const fetchCities = async () => {
        try {
            const res = await fetch('/api/cities');
            if (!res.ok) throw new Error('Failed to fetch cities');
            const data = await res.json();
            setCities(data.map((c: any) => ({ ...c, id: String(c.id) }))); // Convert ID to string for frontend consistency
        } catch (e) {
            console.error(e);
        }
    };

    const fetchPeople = async () => {
        try {
            const res = await fetch('/api/people');
            if (!res.ok) throw new Error('Failed to fetch people');
            const data = await res.json();
            setPeople(data.map((p: any) => ({ ...p, id: String(p.id), cityId: String(p.city_id) })));
        } catch (e) {
            console.error(e);
        }
    };

    const fetchSchedule = async (cityId: string) => {
        try {
            const res = await fetch(`/api/schedules/${cityId}`);
            if (!res.ok) throw new Error('Failed to fetch schedule');
            const data = await res.json();
            if (data.schedule_data) {
                setSchedules(prev => ({ ...prev, [cityId]: data.schedule_data }));
            }
        } catch (e) {
            console.error(e);
        }
    };

    // Load initial data
    useEffect(() => {
        fetchCities();
        fetchPeople();
    }, []);

    // Timer for current time display
    useEffect(() => {
        const timer = setInterval(() => setCurrentTime(new Date()), 60000); // Update every minute
        return () => clearInterval(timer);
    }, []);

    const goBack = () => setView('main');

    const handleAddCity = async (name: string) => {
        try {
            const res = await fetch('/api/cities', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
            if (!res.ok) {
                const err = await res.json();
                alert(err.detail || 'Error adding city');
                return;
            }
            await fetchCities();
        } catch (e) {
            alert('Error adding city');
        }
    };

    const handleDeleteCity = async (cityId: string) => {
        if (window.confirm("Вы уверены? Удаление города также удалит всех людей и графики, связанные с ним.")) {
            try {
                const res = await fetch(`/api/cities/${cityId}`, { method: 'DELETE' });
                if (!res.ok) throw new Error('Failed to delete');
                await fetchCities();
                await fetchPeople(); // People are deleted cascade
                setSchedules(prev => {
                    const newS = { ...prev };
                    delete newS[cityId];
                    return newS;
                });
            } catch (e) {
                alert('Error deleting city');
            }
        }
    };

    const handleAddPerson = async (person: Omit<Person, 'id'>) => {
        try {
            const res = await fetch('/api/people', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: person.name,
                    city_id: parseInt(person.cityId),
                    group: person.group
                })
            });
            if (!res.ok) throw new Error('Failed to add person');
            await fetchPeople();
            setView('manage_people');
        } catch (e) {
            alert('Error adding person');
        }
    };

    const handleUpdatePerson = async (updatedPerson: Person) => {
        try {
            const res = await fetch(`/api/people/${updatedPerson.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: updatedPerson.name,
                    group: updatedPerson.group
                })
            });
            if (!res.ok) throw new Error('Failed to update person');
            await fetchPeople();
            setEditingPerson(null);
            setView('manage_people');
        } catch (e) {
            alert('Error updating person');
        }
    };

    const handleDeletePerson = async (personId: string) => {
        if (window.confirm("Вы уверены, что хотите удалить этого человека?")) {
            try {
                const res = await fetch(`/api/people/${personId}`, { method: 'DELETE' });
                if (!res.ok) throw new Error('Failed to delete person');
                await fetchPeople();
            } catch (e) {
                alert('Error deleting person');
            }
        }
    };

    const handleImageUpload = async (file: File, cityId: string) => {
        setIsLoading(true);
        setError(null);
        try {
            const formData = new FormData();
            formData.append('city_id', cityId);
            formData.append('file', file);

            const res = await fetch('/api/upload_schedule', {
                method: 'POST',
                body: formData
            });

            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Upload failed');
            }

            const data = await res.json();
            setSchedules(prev => ({ ...prev, [cityId]: data.schedule_data }));
            setView('main');
        } catch (e: any) {
            setError(e.message || 'An unknown error occurred.');
        } finally {
            setIsLoading(false);
        }
    };

    const renderHeader = (title: string, showBackButton: boolean = false) => (
        <div className="flex items-center p-4 bg-secondary shadow-md sticky top-0 z-10">
            {showBackButton && (
                <button onClick={goBack} className="mr-4 p-2 rounded-full hover:bg-accent transition-colors">
                    <BackIcon />
                </button>
            )}
            <h1 className="text-xl font-bold text-text-primary">{title}</h1>
        </div>
    );

    const renderMainMenu = () => (
        <div className="p-4">
            {renderHeader('🏠 Главное меню')}
            <div className="grid grid-cols-2 md:grid-cols-3 gap-4 mt-4">
                <MainButton icon={<UserIcon />} label="👤 Управление людьми" onClick={() => setView('manage_people')} />
                <MainButton icon={<CalendarIcon />} label="📅 Посмотреть график" onClick={() => setView('view_schedule')} />
                <MainButton icon={<UploadIcon />} label="📸 Загрузить график" onClick={() => setView('upload_schedule')} />
                <MainButton icon={<CityIcon />} label="🏙️ Управление городами" onClick={() => setView('manage_cities')} />
            </div>
        </div>
    );

    const renderManageCities = () => (
        <div className="p-4 animate-fade-in">
            {renderHeader('🏙️ Управление городами', true)}
            <div className="mt-4 bg-secondary p-4 rounded-lg">
                <h2 className="text-lg font-semibold mb-2">Добавить город</h2>
                <form onSubmit={(e) => {
                    e.preventDefault();
                    const formData = new FormData(e.currentTarget);
                    const name = formData.get('cityName') as string;
                    handleAddCity(name);
                    e.currentTarget.reset();
                }} className="flex gap-2">
                    <input name="cityName" placeholder="Название города" className="w-full bg-primary border border-accent rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-action" />
                    <button type="submit" className="bg-action text-white px-4 py-2 rounded-md font-semibold hover:bg-opacity-80 transition-colors">Добавить</button>
                </form>
            </div>
            <div className="mt-6">
                <h2 className="text-lg font-semibold mb-2">Список городов</h2>
                <ul className="space-y-2">
                    {cities.length > 0 ? cities.map(city => (
                        <li key={city.id} className="bg-secondary p-3 rounded-lg flex justify-between items-center">
                            <span>{city.name}</span>
                            <button onClick={() => handleDeleteCity(city.id)} className="text-danger hover:text-opacity-80 p-1 rounded-full">
                                <TrashIcon />
                            </button>
                        </li>
                    )) : <p className="text-text-secondary">Городов пока нет.</p>}
                </ul>
            </div>
        </div>
    );

    const PersonForm: React.FC<{ person?: Person; onSubmit: (person: any) => void; onCancel: () => void; cities: City[] }> = ({ person, onSubmit, onCancel, cities }) => {
        const [formData, setFormData] = useState({
            name: person?.name || '',
            cityId: person?.cityId || (cities[0]?.id || ''),
            group: person?.group || ELECTRICITY_GROUPS[0],
        });

        const handleSubmit = (e: React.FormEvent) => {
            e.preventDefault();
            if (!formData.name || !formData.cityId || !formData.group) {
                alert("Пожалуйста, заполните все поля.");
                return;
            }
            onSubmit({ id: person?.id, ...formData });
        };

        if (cities.length === 0) return <p className="text-text-secondary mt-4">Сначала добавьте город в разделе "Управление городами".</p>

        // Ensure cityId is valid
        useEffect(() => {
            if (cities.length > 0 && !cities.find(c => c.id === formData.cityId)) {
                setFormData(prev => ({ ...prev, cityId: cities[0].id }));
            }
        }, [cities]);

        return (
            <form onSubmit={handleSubmit} className="space-y-4 bg-secondary p-4 rounded-lg mt-4 animate-slide-in-up">
                <div>
                    <label className="block mb-1 text-sm font-medium">Город</label>
                    <select value={formData.cityId} onChange={e => setFormData(f => ({ ...f, cityId: e.target.value }))} className="w-full bg-primary border border-accent rounded-md px-3 py-2">
                        {cities.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
                    </select>
                </div>
                <div>
                    <label className="block mb-1 text-sm font-medium">Имя</label>
                    <input value={formData.name} onChange={e => setFormData(f => ({ ...f, name: e.target.value }))} placeholder="Богдан" className="w-full bg-primary border border-accent rounded-md px-3 py-2" />
                </div>
                <div>
                    <label className="block mb-1 text-sm font-medium">Группа</label>
                    <select value={formData.group} onChange={e => setFormData(f => ({ ...f, group: e.target.value }))} className="w-full bg-primary border border-accent rounded-md px-3 py-2">
                        {ELECTRICITY_GROUPS.map(g => <option key={g} value={g}>{g}</option>)}
                    </select>
                </div>
                <div className="flex gap-2 justify-end">
                    <button type="button" onClick={onCancel} className="bg-accent text-white px-4 py-2 rounded-md font-semibold hover:bg-opacity-80">Отмена</button>
                    <button type="submit" className="bg-action text-white px-4 py-2 rounded-md font-semibold hover:bg-opacity-80">Сохранить</button>
                </div>
            </form>
        );
    };

    const renderManagePeople = () => (
        <div className="p-4 animate-fade-in">
            {renderHeader('👤 Управление людьми', true)}
            {view !== 'add_person' && view !== 'edit_person' && (
                <button onClick={() => setView('add_person')} className="flex items-center gap-2 mt-4 bg-action text-white px-4 py-2 rounded-md font-semibold hover:bg-opacity-80 transition-colors w-full justify-center">
                    <PlusIcon /> Добавить человека
                </button>
            )}

            {view === 'add_person' && <PersonForm cities={cities} onSubmit={handleAddPerson} onCancel={() => setView('manage_people')} />}
            {view === 'edit_person' && editingPerson && <PersonForm person={editingPerson} cities={cities} onSubmit={handleUpdatePerson} onCancel={() => { setEditingPerson(null); setView('manage_people'); }} />}

            <div className="mt-6">
                <h2 className="text-lg font-semibold mb-2">Список людей</h2>
                <ul className="space-y-2">
                    {people.length > 0 ? people.map(p => {
                        const city = cities.find(c => c.id === p.cityId);
                        return (
                            <li key={p.id} className="bg-secondary p-3 rounded-lg flex justify-between items-center">
                                <div>
                                    <p className="font-bold">{p.name}</p>
                                    <p className="text-sm text-text-secondary">{city?.name || 'Неизвестный город'}, Группа: {p.group}</p>
                                </div>
                                <div className="flex gap-2">
                                    <button onClick={() => { setEditingPerson(p); setView('edit_person'); }} className="text-highlight hover:text-action p-1 rounded-full"><EditIcon /></button>
                                    <button onClick={() => handleDeletePerson(p.id)} className="text-highlight hover:text-danger p-1 rounded-full"><TrashIcon /></button>
                                </div>
                            </li>
                        )
                    }
                    ) : <p className="text-text-secondary">Людей пока нет.</p>}
                </ul>
            </div>
        </div>
    );

    const UploadScheduleComponent: React.FC<{ cities: City[], onUpload: (file: File, cityId: string) => void, isLoading: boolean, error: string | null }> = ({ cities, onUpload, isLoading, error }) => {
        const [selectedCity, setSelectedCity] = useState(cities[0]?.id || '');
        const [file, setFile] = useState<File | null>(null);

        if (cities.length === 0) {
            return <p className="text-text-secondary mt-4">Сначала добавьте город.</p>
        }

        // Update selected city if the list changes and current selection is invalid
        useEffect(() => {
            if (cities.length > 0 && !cities.find(c => c.id === selectedCity)) {
                setSelectedCity(cities[0].id);
            }
        }, [cities]);

        const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
            if (e.target.files && e.target.files[0]) {
                setFile(e.target.files[0]);
            }
        };

        const handleSubmit = () => {
            if (file && selectedCity) {
                onUpload(file, selectedCity);
            } else {
                alert("Пожалуйста, выберите город и файл.");
            }
        }

        return (
            <div className="mt-4 bg-secondary p-4 rounded-lg space-y-4 animate-slide-in-up">
                <div>
                    <label className="block mb-1 text-sm font-medium">Выберите город</label>
                    <select value={selectedCity} onChange={e => setSelectedCity(e.target.value)} className="w-full bg-primary border border-accent rounded-md px-3 py-2">
                        {cities.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
                    </select>
                </div>
                <div>
                    <label className="block mb-1 text-sm font-medium">Загрузите фото графика</label>
                    <input type="file" accept="image/*" onChange={handleFileChange} className="w-full text-sm text-text-secondary file:mr-4 file:py-2 file:px-4 file:rounded-md file:border-0 file:text-sm file:font-semibold file:bg-action file:text-white hover:file:bg-opacity-80" />
                </div>
                {file && <img src={URL.createObjectURL(file)} alt="Preview" className="max-h-40 rounded-md mx-auto" />}
                <button onClick={handleSubmit} disabled={isLoading || !file} className="w-full bg-action text-white px-4 py-2 rounded-md font-semibold hover:bg-opacity-80 disabled:bg-gray-500 disabled:cursor-not-allowed">
                    {isLoading ? '⚙️ Распознавание...' : 'Загрузить и распознать'}
                </button>
                {error && <p className="text-danger text-center">{error}</p>}
            </div>
        );
    }

    const ViewScheduleComponent: React.FC<{ cities: City[], people: Person[], schedules: Schedules, currentTime: Date }> = ({ cities, people, schedules, currentTime }) => {
        const [cityId, setCityId] = useState<string | null>(null);
        const [personId, setPersonId] = useState<string | null>(null);

        const peopleInCity = useMemo(() => people.filter(p => p.cityId === cityId), [people, cityId]);

        useEffect(() => {
            if (cityId && peopleInCity.length > 0) {
                setPersonId(peopleInCity[0].id);
            } else {
                setPersonId(null);
            }
        }, [cityId, peopleInCity]);

        // Fetch schedule when city is selected
        useEffect(() => {
            if (cityId) {
                fetchSchedule(cityId);
            }
        }, [cityId]);

        const selectedPerson = people.find(p => p.id === personId);
        const citySchedule = cityId ? schedules[cityId] : undefined;
        const personScheduleIntervals = selectedPerson ? citySchedule?.[selectedPerson.group] : undefined;

        const status: ScheduleStatus | null = useMemo(() => {
            if (selectedPerson) {
                return getScheduleStatus(personScheduleIntervals, currentTime);
            }
            return null;
        }, [personScheduleIntervals, currentTime, selectedPerson]);


        if (cities.length === 0) return <p className="text-text-secondary mt-4">Сначала добавьте город.</p>;
        if (people.length === 0) return <p className="text-text-secondary mt-4">Сначала добавьте человека.</p>;

        return (
            <div className="mt-4 bg-secondary p-4 rounded-lg space-y-4 animate-slide-in-up">
                <div>
                    <label className="block mb-1 text-sm font-medium">Город</label>
                    <select value={cityId || ''} onChange={e => setCityId(e.target.value)} className="w-full bg-primary border border-accent rounded-md px-3 py-2">
                        <option value="" disabled>Выберите город</option>
                        {cities.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
                    </select>
                </div>
                {cityId && (
                    <div>
                        <label className="block mb-1 text-sm font-medium">Человек</label>
                        <select value={personId || ''} onChange={e => setPersonId(e.target.value)} className="w-full bg-primary border border-accent rounded-md px-3 py-2" disabled={peopleInCity.length === 0}>
                            {peopleInCity.length > 0 ? (
                                peopleInCity.map(p => <option key={p.id} value={p.id}>{p.name}</option>)
                            ) : (
                                <option value="">В этом городе нет людей</option>
                            )}
                        </select>
                    </div>
                )}
                {selectedPerson && status && (
                    <div className="mt-4 border-t border-accent pt-4 space-y-3 animate-fade-in">
                        <div className={`p-4 rounded-lg flex items-center gap-4 ${status.status === 'ON' ? 'bg-success/20 text-success' : 'bg-danger/20 text-danger'}`}>
                            <LightningIcon className="h-8 w-8" />
                            <p className="font-bold text-lg">{status.message}</p>
                        </div>
                        <div className="bg-primary p-4 rounded-lg">
                            <p>👤 {selectedPerson.name} (группа {selectedPerson.group})</p>
                            <p>🏙️ Город: {cities.find(c => c.id === selectedPerson.cityId)?.name}</p>
                            <p className="mt-2">🕐 Текущее время: {currentTime.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</p>
                        </div>
                        <div className="bg-primary p-4 rounded-lg">
                            <h3 className="font-semibold text-lg mb-2">⚡ График отключений на сегодня:</h3>
                            {personScheduleIntervals && personScheduleIntervals.length > 0 ? (
                                <ul className="list-disc list-inside space-y-1">
                                    {personScheduleIntervals.map((interval, i) => <li key={i}>{interval}</li>)}
                                </ul>
                            ) : <p className="text-text-secondary">График для этой группы не загружен.</p>}
                        </div>
                        <div className="bg-primary p-4 rounded-lg">
                            <p className="font-semibold">{status.nextChange}</p>
                            <p className="text-text-secondary">{status.timeToNextChange}</p>
                        </div>
                    </div>
                )}
            </div>
        );
    };

    const renderContent = () => {
        switch (view) {
            case 'manage_cities':
                return renderManageCities();
            case 'manage_people':
            case 'add_person':
            case 'edit_person':
                return renderManagePeople();
            case 'upload_schedule':
                return <div className="p-4"><>{renderHeader('📸 Загрузить график', true)}<UploadScheduleComponent cities={cities} onUpload={handleImageUpload} isLoading={isLoading} error={error} /></></div>
            case 'view_schedule':
                return <div className="p-4"><>{renderHeader('📅 Посмотреть график', true)}<ViewScheduleComponent cities={cities} people={people} schedules={schedules} currentTime={currentTime} /></></div>
            case 'main':
            default:
                return renderMainMenu();
        }
    };

    return (
        <div className="bg-primary min-h-screen text-text-primary font-sans">
            <div className="max-w-2xl mx-auto">
                {renderContent()}
            </div>
        </div>
    );
};

export default App;
