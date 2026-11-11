"use strict";
// Presentation only. Weather is fetched by the original Hermes Weather plugin.
window.ShareletWeather=(()=>{
  const cities=[['tokyo',35.6762,139.6503],['shanghai',31.2304,121.4737],['london',51.5074,-0.1278],['new_york',40.7128,-74.006]];
  function coordinates(value){return value && ['lat','lon'].every(key=>typeof value[key]==='number'&&Number.isFinite(value[key])) && Math.abs(value.lat)<=90 && Math.abs(value.lon)<=180 ? {lat:value.lat,lon:value.lon}:null;}
  function setup(container){
    const lat=container.querySelector('[data-form-field="lat"]'),lon=container.querySelector('[data-form-field="lon"]');
    if(!lat || !lon)return;
    const group=document.createElement('div');group.className='weather-cities';
    const label=document.createElement('p');label.className='field-label';label.dataset.i18n='weather_choose_city';label.textContent=t('weather_choose_city');group.append(label);
    const buttons=document.createElement('div');buttons.className='weather-city-buttons';
    function highlight(){for(const [index,button] of Array.from(buttons.children).entries())button.setAttribute('aria-pressed',String(Math.abs(Number(lat.value)-cities[index][1])<.00001&&Math.abs(Number(lon.value)-cities[index][2])<.00001));}
    for(const [city,a,b] of cities){const button=document.createElement('button');button.type='button';button.className='weather-city';button.dataset.i18n='weather_city_'+city;button.textContent=t('weather_city_'+city);button.onclick=()=>{lat.value=String(a);lon.value=String(b);highlight();};buttons.append(button);}
    group.append(buttons);container.prepend(group);
    lat.min='-90';lat.max='90';lon.min='-180';lon.max='180';
    if(lat.value===''&&lon.value===''){lat.value=String(cities[0][1]);lon.value=String(cities[0][2]);}
    lat.addEventListener('input',highlight);lon.addEventListener('input',highlight);highlight();
  }
  const groups={0:'clear',1:'mostly_clear',2:'partly_cloudy',3:'cloudy',45:'fog',48:'fog',51:'drizzle',53:'drizzle',55:'drizzle',56:'freezing_rain',57:'freezing_rain',61:'rain',63:'rain',65:'rain',66:'freezing_rain',67:'freezing_rain',71:'snow',73:'snow',75:'snow',77:'snow',80:'showers',81:'showers',82:'showers',85:'snow',86:'snow',95:'thunderstorm',96:'thunderstorm',99:'thunderstorm'};
  function accepts(value){return value && typeof value.time==='string'&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value.time)&&
    ['temperature_2m','relative_humidity_2m','apparent_temperature','precipitation','wind_speed_10m','surface_pressure','weather_code'].every(key=>typeof value[key]==='number'&&Number.isFinite(value[key]));}
  function render(value,container,location){
    if(!accepts(value))return false;
    const make=(tag,className,text)=>{const node=document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;};
    const number=value=>new Intl.NumberFormat(I.locale,{maximumFractionDigits:1}).format(value);
    const point=coordinates(location),city=point&&cities.find(([,a,b])=>Math.abs(a-point.lat)<.00001&&Math.abs(b-point.lon)<.00001);
    const kind=groups[value.weather_code],root=make('section','weather-result');
    root.append(make('p','eyebrow',t('weather_current')),make('h2','',city?t('weather_city_'+city[0]):t('weather_custom')));
    if(point)root.append(make('p','weather-coordinates',point.lat.toFixed(4)+'°, '+point.lon.toFixed(4)+'°'));
    root.append(make('div','weather-symbol',kind==='clear'||kind==='mostly_clear'?'☀':kind==='rain'||kind==='showers'?'☂':'☁'));
    root.append(make('div','weather-temperature',number((value.temperature_2m-32)*5/9)+'°C'));
    root.append(make('p','weather-condition',kind?t('weather_condition_'+kind):t('weather_code',{code:value.weather_code})));
    root.append(make('p','weather-feels',t('weather_feels',{value:number((value.apparent_temperature-32)*5/9)})));
    const stats=make('dl','weather-stats');
    for(const [label,text] of [['weather_humidity',number(value.relative_humidity_2m)+'%'],['weather_wind',number(value.wind_speed_10m*1.609344)+' km/h'],['weather_precipitation',number(value.precipitation)+' mm'],['weather_pressure',number(value.surface_pressure)+' hPa']]){
      const item=make('div','');item.append(make('dt','',t(label)),make('dd','',text));stats.append(item);
    }
    root.append(stats,make('p','weather-time',t('weather_updated',{time:value.time.replace('T',' ')})),make('p','weather-source',t('weather_source')));
    const details=make('details','weather-raw');details.append(make('summary','',t('weather_original')),make('pre','',JSON.stringify(value,null,2)));root.append(details);
    container.replaceChildren(root);return true;
  }
  return {setup,render,coordinates,accepts};
})();
